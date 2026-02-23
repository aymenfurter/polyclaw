"""Deployment and infrastructure routes -- /api/setup/infra/*, /api/setup/aca/*."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Coroutine
from typing import Any

from aiohttp import web

from ...config.settings import cfg
from ...services.cloud.azure import AzureCLI
from ...services.cloud.runtime_identity import RuntimeIdentityProvisioner
from ...services.deployment.aca_deployer import AcaDeployer, AcaDeployRequest
from ...services.deployment.provisioner import Provisioner
from ...state.deploy_state import DeployStateStore
from ...state.infra_config import InfraConfigStore
from ...util.async_helpers import run_sync
from ._helpers import error_response as _error, ok_response as _ok

logger = logging.getLogger(__name__)


class DeploymentRoutes:
    """Handles infrastructure provisioning, ACA deployment, runtime identity, lockdown."""

    def __init__(
        self,
        az: AzureCLI,
        provisioner: Provisioner,
        rebuild_adapter: Callable,
        restart_runtime: Callable[[], Coroutine[Any, Any, None]],
        infra_store: InfraConfigStore,
        deploy_store: DeployStateStore | None = None,
        aca_deployer: AcaDeployer | None = None,
    ) -> None:
        self._az = az
        self._provisioner = provisioner
        self._rebuild = rebuild_adapter
        self._restart_runtime = restart_runtime
        self._store = infra_store
        self._deploy_store = deploy_store
        self._aca_deployer = aca_deployer
        self._runtime_identity = RuntimeIdentityProvisioner(az)

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/setup/infra/status", self.infra_status)
        router.add_post("/api/setup/infra/deploy", self.infra_deploy)
        router.add_post("/api/setup/infra/decommission", self.infra_decommission)
        router.add_get("/api/setup/lockdown", self.lockdown_status)
        router.add_post("/api/setup/lockdown", self.lockdown_toggle)
        router.add_get("/api/setup/runtime-identity", self.runtime_identity_status)
        router.add_post("/api/setup/runtime-identity/provision", self.runtime_identity_provision)
        router.add_post("/api/setup/runtime-identity/revoke", self.runtime_identity_revoke)
        router.add_get("/api/setup/aca/status", self.aca_status)
        router.add_post("/api/setup/aca/deploy", self.aca_deploy)
        router.add_post("/api/setup/aca/destroy", self.aca_destroy)
        router.add_post("/api/setup/container/restart", self.container_restart)

    # -- Infrastructure --

    async def infra_status(self, _req: web.Request) -> web.Response:
        result = await run_sync(self._provisioner.status)
        return web.json_response(result)

    async def infra_deploy(self, _req: web.Request) -> web.Response:
        decomm_steps = await run_sync(self._provisioner.decommission)
        prov_steps = await run_sync(self._provisioner.provision)
        self._rebuild()

        all_steps = decomm_steps + prov_steps
        prov_failed = any(s.get("status") == "failed" for s in prov_steps)
        if not prov_failed:
            await self._restart_runtime()
        return web.json_response({
            "status": "error" if prov_failed else "ok",
            "message": "Deploy completed with errors" if prov_failed else "Deployed",
            "steps": all_steps,
        }, status=500 if prov_failed else 200)

    async def infra_decommission(self, _req: web.Request) -> web.Response:
        steps = await run_sync(self._provisioner.decommission)
        self._rebuild()
        failed = any(s.get("status") == "failed" for s in steps)
        return web.json_response({
            "status": "error" if failed else "ok",
            "message": "Errors during decommission" if failed else "Decommissioned",
            "steps": steps,
        }, status=500 if failed else 200)

    # -- Lock Down Mode --

    async def lockdown_status(self, _req: web.Request) -> web.Response:
        return web.json_response({
            "lockdown_mode": cfg.lockdown_mode,
            "tunnel_restricted": cfg.tunnel_restricted,
        })

    async def lockdown_toggle(self, req: web.Request) -> web.Response:
        body = await req.json()
        enabled = bool(body.get("enabled", False))

        if enabled:
            if cfg.lockdown_mode:
                return _ok("Already enabled")
            cfg.write_env(LOCKDOWN_MODE="1", TUNNEL_RESTRICTED="1")
            try:
                self._az.ok("logout")
                self._az.invalidate_cache("account", "show")
            except Exception:
                pass
            return web.json_response({
                "status": "ok", "lockdown_mode": True,
                "message": "Lock Down Mode enabled.",
            })
        else:
            if not cfg.lockdown_mode:
                return _ok("Already disabled")
            cfg.write_env(LOCKDOWN_MODE="", TUNNEL_RESTRICTED="")
            return web.json_response({
                "status": "ok", "lockdown_mode": False,
                "message": "Lock Down Mode disabled.",
            })

    # -- Runtime Identity --

    async def runtime_identity_status(self, _req: web.Request) -> web.Response:
        return web.json_response(self._runtime_identity.status())

    async def runtime_identity_provision(self, req: web.Request) -> web.Response:
        body = await req.json()
        rg = body.get("resource_group") or cfg.env.read("BOT_RESOURCE_GROUP")
        if not rg:
            return _error("resource_group is required (or set BOT_RESOURCE_GROUP)", 400)
        result = await run_sync(self._runtime_identity.provision, rg)
        if result.get("ok"):
            await self._restart_runtime()
        status_code = 200 if result.get("ok") else 500
        return web.json_response(result, status=status_code)

    async def runtime_identity_revoke(self, _req: web.Request) -> web.Response:
        result = await run_sync(self._runtime_identity.revoke)
        return web.json_response(result)

    # -- ACA Deployment --

    async def aca_status(self, _req: web.Request) -> web.Response:
        if not self._aca_deployer:
            return _error("ACA deployer not available", 500)
        return web.json_response(self._aca_deployer.status())

    async def aca_deploy(self, req: web.Request) -> web.Response:
        if not self._aca_deployer:
            return _error("ACA deployer not available", 500)
        body = await req.json()
        aca_req = AcaDeployRequest(
            resource_group=body.get("resource_group", self._store.bot.resource_group),
            location=body.get("location", self._store.bot.location),
            bot_display_name=body.get("display_name", self._store.bot.display_name),
            bot_handle=body.get("bot_handle", self._store.bot.bot_handle),
            admin_port=int(body.get("admin_port", 9090)),
            runtime_port=int(body.get("runtime_port", 8080)),
            image_tag=body.get("image_tag", "latest"),
            acr_name=body.get("acr_name", ""),
            env_name=body.get("env_name", ""),
        )
        result = await run_sync(self._aca_deployer.deploy, aca_req)
        status_code = 200 if result.ok else 500
        return web.json_response({
            "status": "ok" if result.ok else "error",
            "message": "ACA deployment complete" if result.ok else result.error,
            "steps": result.steps,
            "runtime_fqdn": result.runtime_fqdn,
            "deploy_id": result.deploy_id,
        }, status=status_code)

    async def aca_destroy(self, req: web.Request) -> web.Response:
        if not self._aca_deployer:
            return _error("ACA deployer not available", 500)
        body = await req.json() if req.can_read_body else {}
        deploy_id = body.get("deploy_id")
        result = await run_sync(self._aca_deployer.destroy, deploy_id)
        return web.json_response({
            "status": "ok" if result.ok else "error",
            "steps": result.steps,
        })

    async def container_restart(self, _req: web.Request) -> web.Response:
        """Restart the agent container (Docker or ACA) to pick up config changes."""
        deploy_mode = "local"
        if os.getenv("POLYCLAW_USE_MI"):
            deploy_mode = "aca"
        elif os.getenv("POLYCLAW_CONTAINER") == "1":
            deploy_mode = "docker"

        if deploy_mode == "aca":
            if not self._aca_deployer:
                return _error("ACA deployer not available", 500)
            result = await run_sync(self._aca_deployer.restart)
            status_code = 200 if result["ok"] else 500
            return web.json_response({
                "status": "ok" if result["ok"] else "error",
                "message": (
                    "ACA containers restarted" if result["ok"]
                    else "Some containers failed to restart"
                ),
                "deploy_mode": "aca",
                "results": result["results"],
            }, status=status_code)

        if deploy_mode == "docker":
            try:
                proc = subprocess.run(
                    ["docker", "restart", "polyclaw-runtime"],
                    capture_output=True, text=True, timeout=60,
                )
                ok = proc.returncode == 0
                return web.json_response({
                    "status": "ok" if ok else "error",
                    "message": (
                        "Docker runtime container restarted" if ok
                        else proc.stderr.strip()
                    ),
                    "deploy_mode": "docker",
                }, status=200 if ok else 500)
            except Exception as exc:
                logger.warning(
                    "[setup.container_restart] docker restart failed: %s",
                    exc, exc_info=True,
                )
                return _error(f"Docker restart failed: {exc}")

        # Local / combined mode -- reload config in-process
        await self._restart_runtime()
        return web.json_response({
            "status": "ok",
            "message": "Configuration reloaded",
            "deploy_mode": "local",
        })
