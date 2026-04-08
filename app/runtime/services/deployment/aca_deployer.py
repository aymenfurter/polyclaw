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
from ._models import StepTracker
from .aca_provision import (
    assign_rbac,
    configure_ip_whitelist,
    ensure_aca_environment,
    ensure_acr,
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
        steps = StepTracker()
        result = AcaDeployResult(steps=steps._steps)  # noqa: SLF001

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

            acr_name = ensure_acr(self._az, req.resource_group, req.location, steps, rec,
                                   acr_name=req.acr_name)
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
                env_name=req.env_name,
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
            steps.ok("write_aca_config")

            result.ok = True
            logger.info("[aca] Deployment complete: runtime=%s", runtime_fqdn)

        except Exception as exc:
            logger.error("[aca] Deployment failed: %s", exc, exc_info=True)
            result.error = str(exc)
            steps.fail("unexpected_error", detail=str(exc))

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
        steps = StepTracker()
        result = AcaDeployResult(steps=steps._steps)  # noqa: SLF001

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
        steps.ok("clear_aca_config")

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
        self, rg: str, steps: StepTracker, *, step_label: str = "cleanup",
    ) -> list[str]:
        rg_exists = self._az.json("group", "show", "--name", rg, quiet=True)
        if not isinstance(rg_exists, dict):
            logger.info("[aca] Resource group %s does not exist -- nothing to clean", rg)
            return []

        cleaned: list[str] = []

        # (resource_kind, list_cmd, delete_cmd, name_field, extra_delete_args, filter_fn)
        _RESOURCE_TYPES: list[tuple[str, list[str], list[str], str, list[str],
                                    Any]] = [
            ("containerapp",
             ["containerapp", "list", "--resource-group", rg],
             ["containerapp", "delete", "--resource-group", rg, "--yes"],
             "name", [], None),
            ("identity",
             ["identity", "list", "--resource-group", rg],
             ["identity", "delete", "--resource-group", rg],
             "name", [], None),
            ("aca-env",
             ["containerapp", "env", "list", "--resource-group", rg],
             ["containerapp", "env", "delete", "--resource-group", rg, "--yes", "--no-wait"],
             "name", [], None),
            ("acr",
             ["acr", "list", "--resource-group", rg],
             ["acr", "delete", "--resource-group", rg, "--yes"],
             "name", [], None),
            ("log-analytics",
             ["monitor", "log-analytics", "workspace", "list", "--resource-group", rg],
             ["monitor", "log-analytics", "workspace", "delete", "--resource-group", rg,
              "--yes", "--force"],
             "name", ["--workspace-name"], None),
            ("storage",
             ["storage", "account", "list", "--resource-group", rg],
             ["storage", "account", "delete", "--resource-group", rg, "--yes"],
             "name", [],
             lambda r: "polyclaw_deploy" in (r.get("tags") or {})
                       or r.get("kind") == "StorageV2"),
        ]

        for kind, list_cmd, delete_cmd, name_field, extra_del, filter_fn in _RESOURCE_TYPES:
            resources = self._az.json(*list_cmd, quiet=True)
            for res in (resources if isinstance(resources, list) else []):
                name = res.get(name_field, "")
                if not name:
                    continue
                if filter_fn and not filter_fn(res):
                    continue
                logger.info("[aca] Deleting %s: %s", kind, name)
                # Some commands use --name, log-analytics uses --workspace-name
                name_arg = extra_del + [name] if extra_del else ["--name", name]
                ok, _ = self._az.ok(*delete_cmd, *name_arg)
                if ok:
                    cleaned.append(f"{kind}/{name}")
                steps.record(f"{step_label}/{kind}/{name}", ok=ok)

        return cleaned

    def _cleanup_stale_resources(
        self, req: AcaDeployRequest, steps: StepTracker,
    ) -> None:
        logger.info("[aca] Pre-flight: cleaning all ACA resources in %s ...", req.resource_group)
        cleaned = self._delete_aca_resources(req.resource_group, steps, step_label="cleanup")
        if cleaned:
            detail = ", ".join(cleaned)
            logger.info("[aca] Cleaned %d resource(s): %s", len(cleaned), detail)
        else:
            logger.info("[aca] No resources to clean")
            steps.ok("cleanup", detail="nothing to clean")

    def _ensure_resource_group(
        self, req: AcaDeployRequest, steps: StepTracker, rec: DeploymentRecord,
    ) -> bool:
        logger.info("[aca] Step 1/10: Ensuring resource group %s ...", req.resource_group)
        tag_args = ["--tags", f"polyclaw_deploy={rec.tag}"]
        result = self._az.json(
            "group", "create", "--name", req.resource_group,
            "--location", req.location, *tag_args,
        )
        if result:
            steps.ok("resource_group", detail=req.resource_group)
            if req.resource_group not in rec.resource_groups:
                rec.resource_groups.append(req.resource_group)
            return True
        steps.fail("resource_group", detail=self._az.last_stderr)
        return False

    def _load_env_vars(self, steps: StepTracker) -> dict[str, str]:
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
        steps.ok("load_env_vars",
                 detail=f"{count} variable(s), {resolved_count} @kv: resolved")
        return filtered
