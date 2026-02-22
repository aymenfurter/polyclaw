"""Azure Container Apps deployer."""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ..config.settings import cfg
from ..state.deploy_state import DeploymentRecord, DeployStateStore
from ..state.sandbox_config import SandboxConfigStore
from .azure import AzureCLI

logger = logging.getLogger(__name__)

_IMAGE_NAME = "polyclaw"
_LOCAL_IMAGE = "polyclaw:latest"
_MI_NAME = "polyclaw-runtime-mi"
_ENV_NAME_PREFIX = "polyclaw-env"
_BOT_CONTRIBUTOR_ROLE = "Azure Bot Service Contributor Role"
_RG_READER_ROLE = "Reader"
_SESSION_EXECUTOR_ROLE = "Azure ContainerApps Session Executor"


@dataclass
class AcaDeployRequest:

    resource_group: str = "polyclaw-rg"
    location: str = "eastus"
    bot_display_name: str = "polyclaw"
    bot_handle: str = ""
    admin_port: int = 9090
    runtime_port: int = 8080
    image_tag: str = "latest"
    acr_name: str = ""
    env_name: str = ""


@dataclass
class AcaDeployResult:

    ok: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    runtime_fqdn: str = ""
    acr_name: str = ""
    deploy_id: str = ""


class AcaDeployer:

    def __init__(self, az: AzureCLI, deploy_store: DeployStateStore | None = None) -> None:
        self._az = az
        self._deploy_store = deploy_store

    def deploy(self, req: AcaDeployRequest) -> AcaDeployResult:
        steps: list[dict[str, Any]] = []
        result = AcaDeployResult(steps=steps)

        logger.info("[aca] Starting ACA deployment: rg=%s, location=%s", req.resource_group, req.location)

        rec = DeploymentRecord.new(kind="aca")
        result.deploy_id = rec.deploy_id
        if self._deploy_store:
            self._deploy_store.register(rec)

        try:
            self._cleanup_stale_resources(req, steps)

            if not self._ensure_resource_group(req, steps, rec):
                result.error = "Resource group creation failed"
                return result

            env_vars = self._load_env_vars(steps)

            acr_name = self._ensure_acr(req, steps, rec)
            if not acr_name:
                result.error = "Container registry creation failed"
                return result
            result.acr_name = acr_name

            if not self._push_image(acr_name, req.image_tag, steps):
                result.error = "Image push failed"
                return result

            acr_user, acr_pass = self._get_acr_credentials(acr_name)
            if not acr_user:
                result.error = "Could not retrieve ACR admin credentials"
                return result

            mi_id, mi_client_id = self._ensure_managed_identity(req, steps, rec)
            if not mi_id:
                result.error = "Managed identity creation failed"
                return result

            self._assign_rbac(mi_client_id, req.resource_group, steps)

            env_name, env_id = self._ensure_aca_environment(req, steps, rec)
            if not env_name:
                result.error = "Container Apps environment creation failed"
                return result

            runtime_fqdn = self._ensure_runtime_app(
                req, env_id, acr_name, mi_id, mi_client_id,
                acr_user, acr_pass, env_vars, steps, rec,
            )
            if not runtime_fqdn:
                result.error = "Runtime container app creation failed"
                return result
            result.runtime_fqdn = runtime_fqdn

            ip_steps = self._configure_ip_whitelist(req, steps)
            steps.extend(ip_steps)

            runtime_url = f"https://{runtime_fqdn}"
            cfg.write_env(
                ACA_RUNTIME_FQDN=runtime_fqdn,
                ACA_ACR_NAME=acr_name,
                ACA_ENV_NAME=env_name,
                ACA_MI_RESOURCE_ID=mi_id,
                ACA_MI_CLIENT_ID=mi_client_id,
                RUNTIME_URL=runtime_url,
            )
            os.environ["RUNTIME_URL"] = runtime_url
            logger.info("[aca] RUNTIME_URL set to %s", runtime_url)
            steps.append({"step": "write_aca_config", "status": "ok"})

            result.ok = True
            logger.info("[aca] Deployment complete: runtime=%s", runtime_fqdn)

        except Exception as exc:
            logger.error("[aca] Deployment failed: %s", exc, exc_info=True)
            result.error = str(exc)
            steps.append({"step": "unexpected_error", "status": "failed", "detail": str(exc)})

        if self._deploy_store and rec:
            if result.ok:
                rec.config = {
                    "runtime_fqdn": result.runtime_fqdn,
                    "acr_name": result.acr_name,
                }
            else:
                rec.mark_stopped()
            self._deploy_store.update(rec)

        return result

    def destroy(self, deploy_id: str | None = None) -> AcaDeployResult:
        steps: list[dict[str, Any]] = []
        result = AcaDeployResult(steps=steps)

        rec = None
        if deploy_id and self._deploy_store:
            rec = self._deploy_store.get(deploy_id)
        elif self._deploy_store:
            rec = self._deploy_store.current_aca()

        rg = (
            cfg.env.read("BOT_RESOURCE_GROUP")
            or (rec.resource_groups[0] if rec and rec.resource_groups else "")
        )

        if rg:
            cleaned = self._delete_aca_resources(rg, steps, step_label="destroy")
            if cleaned:
                logger.info("[aca] Destroyed %d resource(s): %s",
                            len(cleaned), ", ".join(cleaned))
            else:
                logger.info("[aca] No ACA resources found to destroy in %s", rg)

        cfg.write_env(
            ACA_RUNTIME_FQDN="",
            ACA_ACR_NAME="", ACA_ENV_NAME="",
            ACA_MI_RESOURCE_ID="",
            ACA_MI_CLIENT_ID="",
            RUNTIME_URL="",
        )
        steps.append({"step": "clear_aca_config", "status": "ok"})

        if rec and self._deploy_store:
            rec.mark_destroyed()
            self._deploy_store.update(rec)

        result.ok = True
        return result

    def status(self) -> dict[str, Any]:
        runtime_fqdn = cfg.env.read("ACA_RUNTIME_FQDN")
        return {
            "deployed": bool(runtime_fqdn),
            "runtime_fqdn": runtime_fqdn or None,
            "acr_name": cfg.env.read("ACA_ACR_NAME") or None,
            "env_name": cfg.env.read("ACA_ENV_NAME") or None,
            "mi_client_id": cfg.env.read("ACA_MI_CLIENT_ID") or None,
        }

    def restart(self) -> dict[str, Any]:
        rg = cfg.env.read("BOT_RESOURCE_GROUP") or "polyclaw-rg"
        app_name = "polyclaw-runtime"

        revisions = self._az.json(
            "containerapp", "revision", "list",
            "--name", app_name,
            "--resource-group", rg,
            quiet=True,
        )
        if not revisions or not isinstance(revisions, list):
            ok, msg = self._az.ok(
                "containerapp", "update",
                "--name", app_name,
                "--resource-group", rg,
                "--set-env-vars", f"RESTART_TS={int(time.time())}",
            )
            result_detail = {
                "app": app_name,
                "status": "ok" if ok else "failed",
                "method": "update",
                "detail": msg if not ok else "forced new revision",
            }
            logger.info("[aca.restart] result=%r", result_detail)
            return {"ok": ok, "results": [result_detail]}

        active = next(
            (r["name"] for r in revisions if r.get("properties", {}).get("active")),
            revisions[0].get("name") if revisions else None,
        )
        if not active:
            result_detail = {
                "app": app_name, "status": "failed",
                "method": "revision_restart",
                "detail": "no active revision found",
            }
            logger.info("[aca.restart] result=%r", result_detail)
            return {"ok": False, "results": [result_detail]}

        ok, msg = self._az.ok(
            "containerapp", "revision", "restart",
            "--name", app_name,
            "--resource-group", rg,
            "--revision", active,
        )
        result_detail = {
            "app": app_name,
            "status": "ok" if ok else "failed",
            "method": "revision_restart",
            "detail": active if ok else msg,
        }
        logger.info("[aca.restart] result=%r", result_detail)
        return {"ok": ok, "results": [result_detail]}

    def _delete_aca_resources(
        self, rg: str, steps: list[dict], *, step_label: str = "cleanup",
    ) -> list[str]:
        rg_exists = self._az.json("group", "show", "--name", rg, quiet=True)
        if not isinstance(rg_exists, dict):
            logger.info("[aca] Resource group %s does not exist -- nothing to clean", rg)
            return []

        cleaned: list[str] = []

        apps = self._az.json(
            "containerapp", "list",
            "--resource-group", rg, quiet=True,
        )
        for app in (apps if isinstance(apps, list) else []):
            name = app.get("name", "")
            if not name:
                continue
            logger.info("[aca] Deleting container app: %s (waiting)", name)
            ok, _ = self._az.ok(
                "containerapp", "delete", "--name", name,
                "--resource-group", rg, "--yes",
            )
            if ok:
                cleaned.append(f"containerapp/{name}")
            steps.append({"step": f"{step_label}/containerapp/{name}",
                          "status": "ok" if ok else "failed"})

        identities = self._az.json(
            "identity", "list",
            "--resource-group", rg, quiet=True,
        )
        for mi in (identities if isinstance(identities, list) else []):
            name = mi.get("name", "")
            if not name:
                continue
            logger.info("[aca] Deleting managed identity: %s (waiting)", name)
            ok, _ = self._az.ok(
                "identity", "delete", "--name", name,
                "--resource-group", rg,
            )
            if ok:
                cleaned.append(f"identity/{name}")
            steps.append({"step": f"{step_label}/identity/{name}",
                          "status": "ok" if ok else "failed"})

        envs = self._az.json(
            "containerapp", "env", "list",
            "--resource-group", rg, quiet=True,
        )
        for env in (envs if isinstance(envs, list) else []):
            name = env.get("name", "")
            if not name:
                continue
            logger.info("[aca] Deleting ACA environment: %s (no-wait)", name)
            ok, _ = self._az.ok(
                "containerapp", "env", "delete", "--name", name,
                "--resource-group", rg, "--yes", "--no-wait",
            )
            if ok:
                cleaned.append(f"aca-env/{name}")
            steps.append({"step": f"{step_label}/aca-env/{name}",
                          "status": "ok" if ok else "failed"})

        acrs = self._az.json(
            "acr", "list",
            "--resource-group", rg, quiet=True,
        )
        for acr in (acrs if isinstance(acrs, list) else []):
            name = acr.get("name", "")
            if not name:
                continue
            logger.info("[aca] Deleting ACR: %s", name)
            ok, _ = self._az.ok(
                "acr", "delete", "--name", name,
                "--resource-group", rg, "--yes",
            )
            if ok:
                cleaned.append(f"acr/{name}")
            steps.append({"step": f"{step_label}/acr/{name}",
                          "status": "ok" if ok else "failed"})

        workspaces = self._az.json(
            "monitor", "log-analytics", "workspace", "list",
            "--resource-group", rg, quiet=True,
        )
        for ws in (workspaces if isinstance(workspaces, list) else []):
            name = ws.get("name", "")
            if not name:
                continue
            logger.info("[aca] Deleting Log Analytics workspace: %s", name)
            ok, _ = self._az.ok(
                "monitor", "log-analytics", "workspace", "delete",
                "--workspace-name", name,
                "--resource-group", rg, "--yes", "--force",
            )
            if ok:
                cleaned.append(f"log-analytics/{name}")
            steps.append({"step": f"{step_label}/log-analytics/{name}",
                          "status": "ok" if ok else "failed"})

        storage_accounts = self._az.json(
            "storage", "account", "list",
            "--resource-group", rg, quiet=True,
        )
        for sa in (storage_accounts if isinstance(storage_accounts, list) else []):
            name = sa.get("name", "")
            if not name:
                continue
            tags = sa.get("tags", {}) or {}
            kind = sa.get("kind", "")
            if "polyclaw_deploy" in tags or kind == "StorageV2":
                logger.info("[aca] Deleting storage account: %s", name)
                ok, _ = self._az.ok(
                    "storage", "account", "delete", "--name", name,
                    "--resource-group", rg, "--yes",
                )
                if ok:
                    cleaned.append(f"storage/{name}")
                steps.append({"step": f"{step_label}/storage/{name}",
                              "status": "ok" if ok else "failed"})

        return cleaned

    def _cleanup_stale_resources(
        self, req: AcaDeployRequest, steps: list[dict],
    ) -> None:
        logger.info("[aca] Pre-flight: cleaning all ACA resources in %s ...", req.resource_group)
        cleaned = self._delete_aca_resources(req.resource_group, steps, step_label="cleanup")
        if cleaned:
            detail = ", ".join(cleaned)
            logger.info("[aca] Cleaned %d resource(s): %s", len(cleaned), detail)
        else:
            logger.info("[aca] No resources to clean")
            steps.append({"step": "cleanup", "status": "ok", "detail": "nothing to clean"})

    def _ensure_resource_group(
        self, req: AcaDeployRequest, steps: list[dict], rec: DeploymentRecord,
    ) -> bool:
        logger.info("[aca] Step 1/10: Ensuring resource group %s ...", req.resource_group)
        tag_args = ["--tags", f"polyclaw_deploy={rec.tag}"]
        result = self._az.json(
            "group", "create", "--name", req.resource_group,
            "--location", req.location, *tag_args,
        )
        if result:
            steps.append({"step": "resource_group", "status": "ok", "detail": req.resource_group})
            if req.resource_group not in rec.resource_groups:
                rec.resource_groups.append(req.resource_group)
            return True
        steps.append({"step": "resource_group", "status": "failed", "detail": self._az.last_stderr})
        return False

    def _load_env_vars(self, steps: list[dict]) -> dict[str, str]:
        from .keyvault import is_kv_ref, kv

        env_map = cfg.env.read_all()
        _DEPLOYER_KEYS = frozenset({
            "ACA_RUNTIME_FQDN", "ACA_ACR_NAME", "ACA_ENV_NAME",
            "ACA_STORAGE_ACCOUNT", "ACA_MI_RESOURCE_ID", "ACA_MI_CLIENT_ID",
            "RUNTIME_URL",
        })
        filtered = {k: v for k, v in env_map.items() if k not in _DEPLOYER_KEYS and v}

        resolved_count = 0
        for key, value in list(filtered.items()):
            if is_kv_ref(value):
                try:
                    plaintext = kv.resolve_value(value)
                    if plaintext:
                        filtered[key] = plaintext
                        resolved_count += 1
                        logger.info("[aca] Resolved @kv: ref for %s", key)
                    else:
                        logger.warning(
                            "[aca] @kv: ref for %s resolved to empty -- removing", key,
                        )
                        del filtered[key]
                except Exception:
                    logger.error(
                        "[aca] Failed to resolve @kv: ref for %s -- removing",
                        key, exc_info=True,
                    )
                    del filtered[key]

        count = len(filtered)
        logger.info(
            "[aca] Step 2/10: Loaded %d env var(s) from local .env "
            "(%d @kv: references resolved)",
            count, resolved_count,
        )
        steps.append({"step": "load_env_vars", "status": "ok",
                      "detail": f"{count} variable(s), {resolved_count} @kv: resolved"})
        return filtered

    def _ensure_acr(
        self, req: AcaDeployRequest, steps: list[dict], rec: DeploymentRecord,
    ) -> str:
        logger.info("[aca] Step 3/10: Creating container registry ...")
        acr_name = "polyclaw" + secrets.token_hex(4)
        acr_name = acr_name[:50].replace("-", "")

        result = self._az.json(
            "acr", "create",
            "--resource-group", req.resource_group,
            "--name", acr_name,
            "--sku", "Basic",
            "--admin-enabled", "true",
            "--location", req.location,
        )
        if not result:
            steps.append({
                "step": "acr_create", "status": "failed",
                "detail": self._az.last_stderr,
            })
            return ""
        steps.append({"step": "acr_create", "status": "ok", "detail": acr_name})
        rec.add_resource("acr", req.resource_group, acr_name, "Container registry")
        return acr_name

    def _get_acr_credentials(self, acr_name: str) -> tuple[str, str]:
        creds = self._az.json("acr", "credential", "show", "--name", acr_name)
        if not isinstance(creds, dict):
            return "", ""
        username = creds.get("username", "")
        passwords = creds.get("passwords", [])
        password = passwords[0].get("value", "") if passwords else ""
        return username, password

    def _push_image(
        self, acr_name: str, tag: str, steps: list[dict],
    ) -> bool:
        logger.info("[aca] Step 4/10: Pushing pre-built image to ACR ...")
        local_image = f"{_IMAGE_NAME}:{tag}"
        remote_image = f"{acr_name}.azurecr.io/{_IMAGE_NAME}:{tag}"

        check = subprocess.run(
            ["docker", "image", "inspect", local_image],
            capture_output=True, text=True,
        )
        if check.returncode != 0:
            detail = (
                f"Local image '{local_image}' not found. "
                "Build it first with: docker build --platform linux/amd64 "
                f"-t {local_image} ."
            )
            logger.error("[aca] %s", detail)
            steps.append({"step": "image_push", "status": "failed", "detail": detail})
            return False

        logger.info("[aca] Logging in to ACR %s ...", acr_name)
        ok, msg = self._az.ok("acr", "login", "--name", acr_name)
        if not ok:
            detail = f"ACR login failed: {msg or self._az.last_stderr}"
            logger.error("[aca] %s", detail)
            steps.append({"step": "image_push", "status": "failed", "detail": detail})
            return False

        logger.info("[aca] Tagging %s -> %s", local_image, remote_image)
        tag_result = subprocess.run(
            ["docker", "tag", local_image, remote_image],
            capture_output=True, text=True,
        )
        if tag_result.returncode != 0:
            detail = f"docker tag failed: {tag_result.stderr.strip()}"
            logger.error("[aca] %s", detail)
            steps.append({"step": "image_push", "status": "failed", "detail": detail})
            return False

        logger.info("[aca] Pushing %s (this may take 1-2 minutes) ...", remote_image)
        push_result = subprocess.run(
            ["docker", "push", remote_image],
            capture_output=True, text=True, timeout=600,
        )
        if push_result.returncode != 0:
            detail = f"docker push failed: {push_result.stderr.strip()[:500]}"
            logger.error("[aca] %s", detail)
            steps.append({"step": "image_push", "status": "failed", "detail": detail})
            return False

        logger.info("[aca] Image pushed: %s", remote_image)
        steps.append({"step": "image_push", "status": "ok", "detail": remote_image})
        return True

    def _ensure_managed_identity(
        self, req: AcaDeployRequest, steps: list[dict], rec: DeploymentRecord,
    ) -> tuple[str, str]:
        logger.info("[aca] Step 5/10: Creating managed identity ...")
        result = self._az.json(
            "identity", "create",
            "--name", _MI_NAME,
            "--resource-group", req.resource_group,
            "--location", req.location,
        )
        if not isinstance(result, dict):
            steps.append({"step": "managed_identity", "status": "failed",
                          "detail": self._az.last_stderr})
            return "", ""

        mi_id = result.get("id", "")
        client_id = result.get("clientId", "")
        steps.append({"step": "managed_identity", "status": "ok", "detail": _MI_NAME})
        rec.add_resource("managed_identity", req.resource_group, _MI_NAME,
                         "Runtime scoped identity")
        return mi_id, client_id

    def _assign_rbac(
        self,
        mi_principal_id: str,
        resource_group: str,
        steps: list[dict],
    ) -> None:
        logger.info("[aca] Step 6/10: Assigning RBAC ...")
        account = self._az.account_info()
        sub_id = account.get("id", "") if account else ""
        rg_scope = f"/subscriptions/{sub_id}/resourceGroups/{resource_group}"

        for role in (_BOT_CONTRIBUTOR_ROLE, _RG_READER_ROLE):
            label = role.lower().replace(" ", "_")
            assigned = False
            for attempt in range(4):
                if attempt:
                    delay = 10 * attempt
                    logger.info(
                        "[aca] RBAC retry %d/3 for %s in %ds ...",
                        attempt, label, delay,
                    )
                    time.sleep(delay)
                ok, _msg = self._az.ok(
                    "role", "assignment", "create",
                    "--assignee", mi_principal_id,
                    "--role", role,
                    "--scope", rg_scope,
                )
                if ok or "already exists" in (self._az.last_stderr or "").lower():
                    assigned = True
                    break
            if assigned:
                steps.append({"step": f"rbac_{label}", "status": "ok",
                              "detail": f"{role} on {resource_group}"})
            else:
                steps.append({"step": f"rbac_{label}", "status": "failed",
                              "detail": self._az.last_stderr})

        session_scope = self._session_pool_scope(sub_id)
        if session_scope:
            label = _SESSION_EXECUTOR_ROLE.lower().replace(" ", "_")
            assigned = False
            for attempt in range(4):
                if attempt:
                    delay = 10 * attempt
                    logger.info(
                        "[aca] RBAC retry %d/3 for %s in %ds ...",
                        attempt, label, delay,
                    )
                    time.sleep(delay)
                ok, _msg = self._az.ok(
                    "role", "assignment", "create",
                    "--assignee", mi_principal_id,
                    "--role", _SESSION_EXECUTOR_ROLE,
                    "--scope", session_scope,
                )
                if ok or "already exists" in (self._az.last_stderr or "").lower():
                    assigned = True
                    break
            if assigned:
                steps.append({"step": f"rbac_{label}", "status": "ok",
                              "detail": f"{_SESSION_EXECUTOR_ROLE} on session pool"})
            else:
                steps.append({"step": f"rbac_{label}", "status": "failed",
                              "detail": self._az.last_stderr})

    def _session_pool_scope(self, subscription_id: str) -> str | None:
        try:
            store = SandboxConfigStore()
            pool_id = store.pool_id
            if pool_id:
                return pool_id
            rg = store.resource_group
            name = store.pool_name
            if rg and name:
                return (
                    f"/subscriptions/{subscription_id}/resourceGroups/{rg}"
                    f"/providers/Microsoft.App/sessionPools/{name}"
                )
        except Exception as exc:
            logger.debug("Could not resolve session pool scope: %s", exc)
        return None

    def _ensure_aca_environment(
        self,
        req: AcaDeployRequest,
        steps: list[dict],
        rec: DeploymentRecord,
    ) -> tuple[str, str]:
        logger.info("[aca] Step 7/10: Creating ACA environment ...")
        env_name = f"{_ENV_NAME_PREFIX}-{secrets.token_hex(4)}"

        result = self._az.json(
            "containerapp", "env", "create",
            "--name", env_name,
            "--resource-group", req.resource_group,
            "--location", req.location,
        )
        if not isinstance(result, dict):
            steps.append({
                "step": "aca_environment", "status": "failed",
                "detail": self._az.last_stderr,
            })
            return "", ""

        env_id = result.get("id", "")
        steps.append({"step": "aca_environment", "status": "ok", "detail": env_name})
        rec.add_resource("aca_environment", req.resource_group, env_name,
                         "Container Apps environment")
        return env_name, env_id

    def _ensure_runtime_app(
        self,
        req: AcaDeployRequest,
        env_id: str,
        acr_name: str,
        mi_id: str,
        mi_client_id: str,
        acr_user: str,
        acr_pass: str,
        env_vars: dict[str, str],
        steps: list[dict],
        rec: DeploymentRecord,
    ) -> str:
        app_name = "polyclaw-runtime"
        admin_secret = cfg.admin_secret or secrets.token_urlsafe(24)
        image = f"{acr_name}.azurecr.io/{_IMAGE_NAME}:{req.image_tag}"

        logger.info("[aca] Step 8/10: Creating runtime container app ...")

        _SECRET_ENV_KEYS = frozenset({
            "RUNTIME_SP_PASSWORD", "ACS_CALLBACK_TOKEN",
            "GITHUB_TOKEN", "BOT_APP_PASSWORD",
            "ACS_CONNECTION_STRING", "AZURE_OPENAI_API_KEY",
        })
        _SKIP = frozenset({
            "POLYCLAW_MODE", "POLYCLAW_DATA_DIR", "ADMIN_PORT",
            "ADMIN_SECRET", "POLYCLAW_CONTAINER", "POLYCLAW_USE_MI",
            "AZURE_CLIENT_ID",
        }) | _SECRET_ENV_KEYS
        aca_secrets: dict[str, str] = {
            "admin-secret": admin_secret,
        }
        for env_key in _SECRET_ENV_KEYS:
            secret_name = env_key.lower().replace("_", "-")
            value = env_vars.get(env_key, "")
            if value:
                aca_secrets[secret_name] = value

        env_pairs = [
            "POLYCLAW_MODE=runtime",
            f"ADMIN_PORT={req.runtime_port}",
            "ADMIN_SECRET=secretref:admin-secret",
            "POLYCLAW_CONTAINER=1",
            "POLYCLAW_USE_MI=1",
            f"AZURE_CLIENT_ID={mi_client_id}",
        ]
        for env_key in sorted(_SECRET_ENV_KEYS):
            secret_name = env_key.lower().replace("_", "-")
            if secret_name in aca_secrets:
                env_pairs.append(f"{env_key}=secretref:{secret_name}")

        for key, value in sorted(env_vars.items()):
            if key not in _SKIP and value:
                env_pairs.append(f"{key}={value}")

        logger.info("[aca] Container env vars: %d total (%d via ACA secrets)",
                    len(env_pairs), len(aca_secrets))

        secret_pairs = [f"{name}={value}" for name, value in sorted(aca_secrets.items())]

        create_args: list[str] = [
            "containerapp", "create",
            "--name", app_name,
            "--resource-group", req.resource_group,
            "--environment", env_id,
            "--image", image,
            "--cpu", "2", "--memory", "4Gi",
            "--min-replicas", "1", "--max-replicas", "1",
            "--ingress", "external",
            "--target-port", str(req.runtime_port),
            "--registry-server", f"{acr_name}.azurecr.io",
            "--registry-username", acr_user,
            "--registry-password", acr_pass,
            "--secrets", *secret_pairs,
            "--env-vars", *env_pairs,
        ]

        result = self._az.json(*create_args)
        if not isinstance(result, dict):
            detail = self._az.last_stderr
            logger.error("[aca] containerapp create failed: %s", detail[:1000])
            steps.append({
                "step": "runtime_container_app", "status": "failed",
                "detail": detail[:500],
            })
            return ""

        logger.info("[aca] Assigning managed identity to container app ...")
        id_ok, id_msg = self._az.ok(
            "containerapp", "identity", "assign",
            "--name", app_name,
            "--resource-group", req.resource_group,
            "--user-assigned", mi_id,
        )
        if not id_ok:
            logger.warning("[aca] MI assignment failed (non-fatal): %s", id_msg)

        fqdn = result.get("properties", {}).get("configuration", {}).get(
            "ingress", {}
        ).get("fqdn", "")

        if fqdn:
            bot_endpoint = f"https://{fqdn}/api/messages"
            self._az.ok(
                "containerapp", "update",
                "--name", app_name,
                "--resource-group", req.resource_group,
                "--set-env-vars", f"BOT_ENDPOINT={bot_endpoint}",
            )

        steps.append({"step": "runtime_container_app", "status": "ok", "detail": fqdn})
        rec.add_resource("container_app", req.resource_group, app_name,
                         "Runtime data plane (MI-scoped)")
        return fqdn

    def _configure_ip_whitelist(
        self,
        req: AcaDeployRequest,
        steps: list[dict],
    ) -> list[dict[str, Any]]:
        ip_steps: list[dict[str, Any]] = []

        public_ip = self._detect_public_ip()
        if not public_ip:
            ip_steps.append({
                "step": "ip_whitelist",
                "status": "skipped",
                "detail": "Could not detect public IP -- runtime ingress unrestricted",
            })
            return ip_steps

        ok, msg = self._az.ok(
            "containerapp", "ingress", "access-restriction", "set",
            "--name", "polyclaw-runtime",
            "--resource-group", req.resource_group,
            "--rule-name", "allow-deployer",
            "--ip-address", f"{public_ip}/32",
            "--action", "Allow",
            "--description", "Allow deployer IP",
        )
        if ok:
            ip_steps.append({
                "step": "ip_whitelist",
                "status": "ok",
                "detail": f"Runtime restricted to {public_ip}/32",
            })
        else:
            ip_steps.append({
                "step": "ip_whitelist",
                "status": "warning",
                "detail": f"Could not set IP restriction: {msg}",
            })

        return ip_steps

    @staticmethod
    def _detect_public_ip() -> str:
        import urllib.request

        for url in (
            "https://api.ipify.org",
            "https://ifconfig.me/ip",
            "https://checkip.amazonaws.com",
        ):
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    ip = resp.read().decode().strip()
                    if ip and "." in ip:
                        return ip
            except Exception:
                continue
        return ""
