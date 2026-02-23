"""Environment / deployment management API routes -- /api/environments/*."""

from __future__ import annotations

import logging

from aiohttp import web

from ...services.cloud.azure import AzureCLI
from ...services.security.misconfig_checker import MisconfigChecker
from ...services.resource_tracker import ResourceTracker
from ...state.deploy_state import DeployStateStore
from ...util.async_helpers import run_sync
from ._helpers import no_az as _no_az

logger = logging.getLogger(__name__)


class EnvironmentRoutes:
    """REST handler for Azure deployment environment management."""

    def __init__(
        self,
        deploy_store: DeployStateStore,
        az: AzureCLI | None = None,
    ) -> None:
        self._store = deploy_store
        self._az = az

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/environments", self._list)
        router.add_get("/api/environments/{deploy_id}", self._get)
        router.add_delete("/api/environments/{deploy_id}", self._destroy)
        router.add_post("/api/environments/{deploy_id}/cleanup", self._cleanup)
        router.add_delete("/api/environments/{deploy_id}/record", self._remove_record)
        router.add_get("/api/environments/audit", self._audit)
        router.add_post("/api/environments/audit/cleanup", self._audit_cleanup)
        router.add_post("/api/environments/misconfig", self._misconfig_check)

    async def _list(self, _req: web.Request) -> web.Response:
        return web.json_response(self._store.summary())

    async def _get(self, req: web.Request) -> web.Response:
        deploy_id = req.match_info["deploy_id"]
        rec = self._store.get(deploy_id)
        if not rec:
            return web.json_response(
                {"status": "error", "message": "Deployment not found"}, status=404
            )
        from dataclasses import asdict
        return web.json_response(asdict(rec))

    async def _destroy(self, req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        deploy_id = req.match_info["deploy_id"]
        tracker = ResourceTracker(self._az, self._store)
        steps = await run_sync(tracker.cleanup_deployment, deploy_id)
        return web.json_response({"status": "ok", "steps": steps})

    async def _cleanup(self, req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        deploy_id = req.match_info["deploy_id"]
        tracker = ResourceTracker(self._az, self._store)
        steps = await run_sync(tracker.cleanup_deployment, deploy_id)
        return web.json_response({"status": "ok", "steps": steps})

    async def _remove_record(self, req: web.Request) -> web.Response:
        deploy_id = req.match_info["deploy_id"]
        if self._store.remove(deploy_id):
            return web.json_response({"status": "ok"})
        return web.json_response(
            {"status": "error", "message": "Not found"}, status=404
        )

    async def _audit(self, _req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        tracker = ResourceTracker(self._az, self._store)
        result = await run_sync(tracker.audit)
        return web.json_response(tracker.to_dict(result))

    async def _audit_cleanup(self, _req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        tracker = ResourceTracker(self._az, self._store)
        result = await run_sync(tracker.audit)
        steps = []
        for group in result.orphaned_groups:
            ok, msg = await run_sync(tracker.cleanup_orphan_group, group.name)
            steps.append({
                "group": group.name,
                "status": "ok" if ok else "failed",
                "detail": msg or "",
            })
        return web.json_response({"status": "ok", "steps": steps})

    async def _misconfig_check(self, req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        body = await req.json() if req.can_read_body else {}
        deploy_id = body.get("deploy_id")

        resource_groups: list[str] = []
        if deploy_id:
            rec = self._store.get(deploy_id)
            if rec:
                resource_groups = rec.resource_groups
        else:
            for rec in self._store.all_deployments.values():
                resource_groups.extend(rec.resource_groups)
            resource_groups = list(set(resource_groups))

        if not resource_groups:
            return web.json_response({"status": "ok", "message": "No resource groups"})

        checker = MisconfigChecker(self._az)
        result = await run_sync(checker.check_all, resource_groups)
        return web.json_response(MisconfigChecker.to_dict(result))

