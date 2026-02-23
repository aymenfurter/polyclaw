"""Azure Container Apps deployer."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ...config.settings import cfg
from ...state.deploy_state import DeploymentRecord, DeployStateStore
from ..cloud.azure import AzureCLI
from ..cloud._azure_rbac import IMAGE_NAME as _IMAGE_NAME
from .aca_provision import (
    assign_rbac,
    configure_ip_whitelist,
    ensure_acr,
    ensure_aca_environment,
    ensure_managed_identity,
    ensure_runtime_app,
    get_acr_credentials,
    push_image,
)

logger = logging.getLogger(__name__)

_LOCAL_IMAGE = "polyclaw:latest"


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

            acr_name = ensure_acr(self._az, req.resource_group, req.location, steps, rec)
            if not acr_name:
                result.error = "Container registry creation failed"
                return result
            result.acr_name = acr_name

            if not push_image(self._az, acr_name, req.image_tag, steps):
                result.error = "Image push failed"
                return result

            acr_user, acr_pass = get_acr_credentials(self._az, acr_name)
            if not acr_user:
                result.error = "Could not retrieve ACR admin credentials"
                return result

            mi_id, mi_client_id = ensure_managed_identity(
                self._az, req.resource_group, req.location, steps, rec,
            )
            if not mi_id:
                result.error = "Managed identity creation failed"
                return result

            assign_rbac(self._az, mi_client_id, req.resource_group, steps)

            env_name, env_id = ensure_aca_environment(
                self._az, req.resource_group, req.location, steps, rec,
            )
            if not env_name:
                result.error = "Container Apps environment creation failed"
                return result

            runtime_fqdn = ensure_runtime_app(
                self._az, req.resource_group, env_id, acr_name,
                mi_id, mi_client_id, acr_user, acr_pass,
                env_vars, req.image_tag, req.runtime_port, steps, rec,
            )
            if not runtime_fqdn:
                result.error = "Runtime container app creation failed"
                return result
            result.runtime_fqdn = runtime_fqdn

            ip_steps = configure_ip_whitelist(self._az, req.resource_group)
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
        from ..keyvault import is_kv_ref, kv

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
