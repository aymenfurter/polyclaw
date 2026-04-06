"""Foundry infrastructure routes -- /api/setup/foundry/*.

Single entry point for all infrastructure provisioning via Bicep template.
Replaces the scattered ``az`` CLI provisioning with one clean deployment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from ...config.settings import cfg
from ...services.cloud.azure import AzureCLI
from ...services.deployment.bicep_deployer import BicepDeployer, BicepDeployRequest
from ...state.deploy_state import DeployStateStore
from ...util.async_helpers import run_sync
from ._helpers import error_response as _error, ok_response as _ok

logger = logging.getLogger(__name__)


class FoundryDeployRoutes:
    """Handles Foundry infrastructure provisioning via Bicep."""

    def __init__(
        self,
        az: AzureCLI,
        deploy_store: DeployStateStore,
        restart_runtime: Any = None,
    ) -> None:
        self._az = az
        self._deployer = BicepDeployer(az, deploy_store)
        self._restart_runtime = restart_runtime

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/setup/foundry/status", self.foundry_status)
        router.add_post("/api/setup/foundry/deploy", self.foundry_deploy)
        router.add_get("/api/setup/foundry/deploy/stream", self.foundry_deploy_stream)
        router.add_post("/api/setup/foundry/decommission", self.foundry_decommission)

    async def foundry_status(self, _req: web.Request) -> web.Response:
        status = self._deployer.status()
        return web.json_response(status)

    async def foundry_deploy(self, req: web.Request) -> web.Response:
        body = await req.json() if req.can_read_body else {}
        deploy_req = BicepDeployRequest(
            resource_group=body.get("resource_group", "polyclaw-rg"),
            location=body.get("location", "eastus"),
            base_name=body.get("base_name", ""),
            deploy_key_vault=body.get("deploy_key_vault", True),
            deploy_acs=body.get("deploy_acs", False),
            deploy_content_safety=body.get("deploy_content_safety", False),
            deploy_search=body.get("deploy_search", False),
            deploy_embedding_aoai=body.get("deploy_embedding_aoai", False),
            deploy_monitoring=body.get("deploy_monitoring", False),
            deploy_session_pool=body.get("deploy_session_pool", False),
        )
        if body.get("models"):
            deploy_req.models = body["models"]

        result = await run_sync(self._deployer.deploy, deploy_req)

        if result.ok and self._restart_runtime:
            try:
                await self._restart_runtime()
            except Exception:
                logger.warning("Failed to restart runtime after deploy", exc_info=True)

        return web.json_response({
            "status": "ok" if result.ok else "error",
            "deploy_id": result.deploy_id,
            "foundry_endpoint": result.foundry_endpoint,
            "foundry_name": result.foundry_name,
            "deployed_models": result.deployed_models,
            "key_vault_url": result.key_vault_url,
            "steps": result.steps,
            "error": result.error,
        }, status=200 if result.ok else 500)

    async def foundry_deploy_stream(self, req: web.Request) -> web.StreamResponse:
        """SSE endpoint that streams deployment progress in real time.

        The deploy config is passed as query-string JSON (``?config={...}``).
        Each step is sent as ``data: {json}\n\n``.  A final ``event: done``
        message carries the full result.
        """
        config_raw = req.query.get("config", "{}")
        try:
            body = json.loads(config_raw)
        except json.JSONDecodeError:
            body = {}

        deploy_req = BicepDeployRequest(
            resource_group=body.get("resource_group", "polyclaw-rg"),
            location=body.get("location", "eastus"),
            base_name=body.get("base_name", ""),
            deploy_key_vault=body.get("deploy_key_vault", True),
            deploy_acs=body.get("deploy_acs", False),
            deploy_content_safety=body.get("deploy_content_safety", False),
            deploy_search=body.get("deploy_search", False),
            deploy_embedding_aoai=body.get("deploy_embedding_aoai", False),
            deploy_monitoring=body.get("deploy_monitoring", False),
            deploy_session_pool=body.get("deploy_session_pool", False),
        )
        if body.get("models"):
            deploy_req.models = body["models"]

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(req)

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()

        def _on_step(step: dict[str, str]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, step)

        async def _run_deploy() -> Any:
            try:
                return await run_sync(self._deployer.deploy, deploy_req, _on_step)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.ensure_future(_run_deploy())

        # Stream steps as they arrive; None signals completion
        while True:
            step = await queue.get()
            if step is None:
                break
            try:
                await resp.write(
                    ("data: %s\n\n" % json.dumps(step)).encode()
                )
            except ConnectionResetError:
                task.cancel()
                return resp

        result = await task

        if result.ok and self._restart_runtime:
            try:
                await self._restart_runtime()
                await resp.write(
                    ("data: %s\n\n" % json.dumps(
                        {"step": "restart_runtime", "status": "ok"}
                    )).encode()
                )
            except Exception:
                logger.warning("Failed to restart runtime after deploy", exc_info=True)

        # Final done event
        done_payload = json.dumps({
            "status": "ok" if result.ok else "error",
            "deploy_id": result.deploy_id,
            "error": result.error,
        })
        await resp.write(("event: done\ndata: %s\n\n" % done_payload).encode())
        await resp.write_eof()
        return resp

    async def foundry_decommission(self, req: web.Request) -> web.Response:
        body = await req.json() if req.can_read_body else {}
        rg = body.get("resource_group", "")
        steps = await run_sync(self._deployer.decommission, rg)
        has_failure = any(s.get("status") == "failed" for s in steps)

        if not has_failure and self._restart_runtime:
            try:
                await self._restart_runtime()
            except Exception:
                logger.warning("Failed to restart runtime after decommission", exc_info=True)

        return web.json_response({
            "status": "error" if has_failure else "ok",
            "steps": steps,
        }, status=500 if has_failure else 200)
