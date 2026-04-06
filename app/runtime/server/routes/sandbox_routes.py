"""Agent Sandbox configuration API routes -- /api/sandbox/*."""

from __future__ import annotations

import logging
import secrets as _secrets
from typing import Any

from aiohttp import web

from ...sandbox import SandboxExecutor
from ...services.cloud.azure import AzureCLI
from ...services.deployment.bicep_deployer import BicepDeployer, BicepDeployRequest
from ...state.deploy_state import DeployStateStore
from ...state.sandbox_config import BLACKLIST, DEFAULT_WHITELIST, SandboxConfigStore
from ...util.async_helpers import run_sync
from ._helpers import fail_response as _fail_response, no_az as _no_az

logger = logging.getLogger(__name__)

_DEFAULT_SANDBOX_RG = "polyclaw-sandbox-rg"


class SandboxRoutes:
    """Admin routes for the Agent Sandbox feature (experimental)."""

    def __init__(
        self,
        config_store: SandboxConfigStore,
        executor: SandboxExecutor,
        az: AzureCLI | None = None,
        deploy_store: DeployStateStore | None = None,
    ) -> None:
        self._store = config_store
        self._executor = executor
        self._az = az
        self._deploy_store = deploy_store
        self._bicep = BicepDeployer(az, deploy_store) if az and deploy_store else None

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/sandbox/config", self.get_config)
        router.add_post("/api/sandbox/config", self.update_config)
        router.add_post("/api/sandbox/test", self.test_sandbox)
        router.add_post("/api/sandbox/provision", self.provision_pool)
        router.add_delete("/api/sandbox/provision", self.remove_pool)

    async def get_config(self, _req: web.Request) -> web.Response:
        data = self._store.to_dict()
        data["blacklist"] = sorted(BLACKLIST)
        data["default_whitelist"] = DEFAULT_WHITELIST
        data["is_provisioned"] = self._store.is_provisioned
        data["experimental"] = True
        data["warnings"] = [
            "Agent Sandbox is an experimental feature and may change or be "
            "removed in future releases.",
            "Do not run multiple chat sessions working on the same files "
            "in parallel. Data is synced back on session teardown using "
            "last-writer-wins -- concurrent changes will be overwritten.",
        ]
        return web.json_response({"status": "ok", **data})

    async def update_config(self, req: web.Request) -> web.Response:
        try:
            body = await req.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "Invalid JSON"}, status=400
            )

        if "enabled" in body:
            self._store.set_enabled(bool(body["enabled"]))
        if "sync_data" in body:
            self._store.set_sync_data(bool(body["sync_data"]))
        if "session_pool_endpoint" in body:
            self._store.set_session_pool_endpoint(str(body["session_pool_endpoint"]))

        if "whitelist" in body:
            wl = body["whitelist"]
            if not isinstance(wl, list):
                return web.json_response(
                    {"status": "error", "message": "whitelist must be a list"},
                    status=400,
                )
            self._store.set_whitelist(wl)

        if "add_whitelist" in body:
            item = str(body["add_whitelist"])
            if not self._store.add_whitelist_item(item):
                return web.json_response(
                    {"status": "error", "message": f"'{item}' is blacklisted"},
                    status=400,
                )

        if "remove_whitelist" in body:
            self._store.remove_whitelist_item(str(body["remove_whitelist"]))
        if body.get("reset_whitelist"):
            self._store.reset_whitelist()

        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def test_sandbox(self, req: web.Request) -> web.Response:
        if not self._store.session_pool_endpoint:
            return web.json_response(
                {"status": "error", "message": "Session pool endpoint not configured"},
                status=400,
            )
        try:
            body = await req.json()
        except Exception:
            body = {}

        command = body.get("command", 'echo "Sandbox is working! $(date)"')
        result = await self._executor.execute(
            command,
            env_vars=body.get("env_vars"),
            timeout=body.get("timeout", 60),
        )
        return web.json_response(
            {"status": "ok" if result["success"] else "error", **result}
        )

    async def provision_pool(self, req: web.Request) -> web.Response:
        if not self._bicep:
            return _no_az()
        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:
            body = {}

        location = body.get("location", "eastus").strip()
        rg = body.get("resource_group", "").strip() or _DEFAULT_SANDBOX_RG

        if self._store.is_provisioned:
            return web.json_response({
                "status": "ok",
                "message": f"Already provisioned: {self._store.pool_name}",
                "steps": [],
                **self._store.to_dict(),
                "is_provisioned": True,
            })

        bicep_req = BicepDeployRequest(
            resource_group=rg,
            location=location,
            deploy_foundry=False,
            deploy_key_vault=False,
            deploy_session_pool=True,
        )
        result = await run_sync(self._bicep.deploy, bicep_req)

        if not result.ok or not result.session_pool_endpoint:
            return _fail_response(result.steps)

        self._store.set_pool_metadata(
            resource_group=rg,
            location=location,
            pool_name=result.session_pool_name,
            pool_id=result.session_pool_id,
            endpoint=result.session_pool_endpoint,
        )
        result.steps.append({
            "step": "save_config", "status": "ok", "detail": "Configuration saved"
        })

        logger.info("Sandbox pool provisioned (Bicep): %s (rg=%s)", result.session_pool_name, rg)
        return web.json_response({
            "status": "ok",
            "message": f"Session pool '{result.session_pool_name}' provisioned",
            "steps": result.steps,
            **self._store.to_dict(),
            "is_provisioned": True,
        })

    async def remove_pool(self, _req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        if not self._store.is_provisioned:
            return web.json_response(
                {"status": "error", "message": "No pool provisioned"}, status=400
            )

        steps: list[dict[str, Any]] = []
        pool_name = self._store.pool_name
        rg = self._store.resource_group

        ok, msg = await run_sync(
            self._az.ok,
            "containerapp", "sessionpool", "delete",
            "--name", pool_name, "--resource-group", rg, "--yes",
        )
        steps.append({
            "step": "delete_pool",
            "status": "ok" if ok else "failed",
            "detail": f"Deleted {pool_name}" if ok else (msg or "Unknown error"),
        })

        if rg == _DEFAULT_SANDBOX_RG:
            rg_ok, rg_msg = await run_sync(
                self._az.ok,
                "group", "delete", "--name", rg, "--yes", "--no-wait",
            )
            steps.append({
                "step": "delete_rg",
                "status": "ok" if rg_ok else "failed",
                "detail": f"Deleting {rg}" if rg_ok else (rg_msg or "Unknown error"),
            })

        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.resources = [
                    r for r in rec.resources if r.resource_name != pool_name
                ]
                if rg in rec.resource_groups:
                    rec.resource_groups.remove(rg)
                self._deploy_store.update(rec)

        self._store.clear_pool_metadata()
        self._store.set_enabled(False)
        steps.append({
            "step": "clear_config", "status": "ok", "detail": "Configuration cleared"
        })

        logger.info("Sandbox pool removed: %s", pool_name)
        return web.json_response({
            "status": "ok",
            "message": f"Session pool '{pool_name}' removed",
            "steps": steps,
            **self._store.to_dict(),
            "is_provisioned": False,
        })
