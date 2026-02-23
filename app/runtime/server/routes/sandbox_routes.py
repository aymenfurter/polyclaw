"""Agent Sandbox configuration API routes -- /api/sandbox/*."""

from __future__ import annotations

import logging
import secrets as _secrets
from typing import Any

from aiohttp import web

from ...sandbox import SandboxExecutor
from ...services.cloud.azure import AzureCLI
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
        if not self._az:
            return _no_az()
        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:
            body = {}

        location = body.get("location", "eastus").strip()
        rg = body.get("resource_group", "").strip() or _DEFAULT_SANDBOX_RG
        pool_name = (
            body.get("pool_name", "").strip()
            or f"polyclaw-sandbox-{_secrets.token_hex(4)}"
        )

        steps: list[dict[str, Any]] = []

        if self._store.is_provisioned:
            return web.json_response({
                "status": "ok",
                "message": f"Already provisioned: {self._store.pool_name}",
                "steps": [],
                **self._store.to_dict(),
                "is_provisioned": True,
            })

        if not await self._ensure_rg(rg, location, steps):
            return _fail_response(steps)

        pool_result = await self._create_pool(rg, location, pool_name, steps)
        if not pool_result:
            return _fail_response(steps)

        endpoint, pool_id = pool_result
        self._store.set_pool_metadata(
            resource_group=rg,
            location=location,
            pool_name=pool_name,
            pool_id=pool_id,
            endpoint=endpoint,
        )
        steps.append({
            "step": "save_config", "status": "ok", "detail": "Configuration saved"
        })

        logger.info("Sandbox pool provisioned: %s (rg=%s)", pool_name, rg)
        return web.json_response({
            "status": "ok",
            "message": f"Session pool '{pool_name}' provisioned",
            "steps": steps,
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

    # -- internal helpers --

    async def _ensure_rg(
        self, rg: str, location: str, steps: list[dict[str, Any]]
    ) -> bool:
        existing = await run_sync(self._az.json, "group", "show", "--name", rg)
        if existing:
            steps.append({
                "step": "resource_group", "status": "ok", "detail": f"{rg} (existing)"
            })
            return True

        tag_args: list[str] = []
        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                tag_args = ["--tags", f"polyclaw_deploy={rec.tag}"]

        result = await run_sync(
            self._az.json,
            "group", "create", "--name", rg, "--location", location, *tag_args,
        )
        ok = bool(result)
        steps.append({
            "step": "resource_group",
            "status": "ok" if ok else "failed",
            "detail": rg if ok else (self._az.last_stderr or "Unknown error"),
        })
        if ok and self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec and rg not in rec.resource_groups:
                rec.resource_groups.append(rg)
                self._deploy_store.update(rec)
        return ok

    async def _create_pool(
        self,
        rg: str,
        location: str,
        pool_name: str,
        steps: list[dict[str, Any]],
    ) -> tuple[str, str] | None:
        logger.info("Creating session pool '%s' in rg '%s'...", pool_name, rg)
        result = await run_sync(
            self._az.json,
            "containerapp", "sessionpool", "create",
            "--name", pool_name, "--resource-group", rg,
            "--location", location, "--container-type", "PythonLTS",
            "--cooldown-period", "300",
        )
        if not result or not isinstance(result, dict):
            err = self._az.last_stderr or "Unknown error"
            steps.append({
                "step": "create_pool", "status": "failed", "detail": err[:300]
            })
            return None

        props = result.get("properties", {})
        endpoint = props.get("poolManagementEndpoint", "")
        pool_id = result.get("id", "")
        if not endpoint:
            endpoint = (
                f"https://{location}.dynamicsessions.io"
                f"/subscriptions/pools/{pool_name}"
            )

        steps.append({
            "step": "create_pool", "status": "ok",
            "detail": f"{pool_name} -> {endpoint}",
        })

        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.add_resource(
                    resource_type="session_pool",
                    resource_group=rg,
                    resource_name=pool_name,
                    purpose="Agent sandbox session pool",
                    resource_id=pool_id,
                )
                self._deploy_store.update(rec)

        return endpoint, pool_id

