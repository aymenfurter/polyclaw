"""Security preflight API routes -- /api/guardrails/preflight."""

from __future__ import annotations

import logging

from aiohttp import web

from ...services.security_preflight import SecurityPreflightChecker
from ...util.async_helpers import run_sync

logger = logging.getLogger(__name__)


class SecurityPreflightRoutes:
    """REST handler for security preflight checks on the admin container."""

    def __init__(self, checker: SecurityPreflightChecker) -> None:
        self._checker = checker
        self._last_result: dict | None = None

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/guardrails/preflight", self._get_status)
        router.add_post("/api/guardrails/preflight/run", self._run_checks)

    async def _get_status(self, _req: web.Request) -> web.Response:
        """Return the most recent preflight results (or empty)."""
        if self._last_result:
            return web.json_response({"status": "ok", **self._last_result})
        return web.json_response({
            "status": "ok", "checks": [], "run_at": None,
            "passed": 0, "failed": 0, "warnings": 0, "skipped": 0,
        })

    async def _run_checks(self, _req: web.Request) -> web.Response:
        """Run all security preflight checks and return evidence."""
        result = await run_sync(self._checker.run_all)
        self._last_result = SecurityPreflightChecker.to_dict(result)
        return web.json_response({"status": "ok", **self._last_result})
