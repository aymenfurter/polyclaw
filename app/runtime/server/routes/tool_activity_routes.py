"""Tool activity API routes -- /api/tool-activity/*."""

from __future__ import annotations

import logging

from aiohttp import web

from ...state.session_store import SessionStore
from ...state.tool_activity_store import ToolActivityStore

logger = logging.getLogger(__name__)


class ToolActivityRoutes:
    """REST handler for tool activity audit log."""

    def __init__(
        self,
        activity_store: ToolActivityStore,
        session_store: SessionStore,
    ) -> None:
        self._store = activity_store
        self._sessions = session_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/tool-activity", self._list)
        router.add_get("/api/tool-activity/summary", self._summary)
        router.add_get("/api/tool-activity/timeline", self._timeline)
        router.add_get("/api/tool-activity/sessions", self._sessions_breakdown)
        router.add_get("/api/tool-activity/export", self._export)
        router.add_get("/api/tool-activity/{entry_id}", self._get)
        router.add_post("/api/tool-activity/{entry_id}/flag", self._flag)
        router.add_post("/api/tool-activity/{entry_id}/unflag", self._unflag)
        router.add_post("/api/tool-activity/import", self._import)

    async def _list(self, req: web.Request) -> web.Response:
        """List tool activity with optional filters."""
        params = req.query
        result = self._store.query(
            session_id=params.get("session_id", ""),
            tool=params.get("tool", ""),
            category=params.get("category", ""),
            status=params.get("status", ""),
            flagged_only=params.get("flagged", "").lower() in ("1", "true", "yes"),
            since=float(params.get("since", "0")),
            model=params.get("model", ""),
            interaction_type=params.get("interaction_type", ""),
            limit=min(int(params.get("limit", "200")), 1000),
            offset=int(params.get("offset", "0")),
        )
        return web.json_response({"status": "ok", **result})

    async def _summary(self, _req: web.Request) -> web.Response:
        """Return aggregate statistics."""
        summary = self._store.get_summary()
        return web.json_response({"status": "ok", **summary})

    async def _get(self, req: web.Request) -> web.Response:
        """Get a single tool activity entry."""
        entry_id = req.match_info["entry_id"]
        entry = self._store.get_entry(entry_id)
        if not entry:
            return web.json_response(
                {"status": "error", "message": "Entry not found"}, status=404,
            )
        return web.json_response({"status": "ok", "entry": entry})

    async def _flag(self, req: web.Request) -> web.Response:
        """Manually flag an entry as suspicious."""
        entry_id = req.match_info["entry_id"]
        body = await req.json()
        reason = body.get("reason", "")
        if self._store.flag_entry(entry_id, reason):
            return web.json_response({"status": "ok"})
        return web.json_response(
            {"status": "error", "message": "Entry not found"}, status=404,
        )

    async def _unflag(self, req: web.Request) -> web.Response:
        """Remove flag from an entry."""
        entry_id = req.match_info["entry_id"]
        if self._store.unflag_entry(entry_id):
            return web.json_response({"status": "ok"})
        return web.json_response(
            {"status": "error", "message": "Entry not found"}, status=404,
        )

    async def _timeline(self, req: web.Request) -> web.Response:
        """Return time-bucketed tool activity data."""
        params = req.query
        data = self._store.get_timeline(
            bucket_minutes=int(params.get("bucket", "60")),
            since=float(params.get("since", "0")),
            until=float(params.get("until", "0")),
        )
        return web.json_response({"status": "ok", "buckets": data})

    async def _sessions_breakdown(self, _req: web.Request) -> web.Response:
        """Return per-session aggregation."""
        data = self._store.get_session_breakdown()
        return web.json_response({"status": "ok", "sessions": data})

    async def _export(self, req: web.Request) -> web.Response:
        """Export tool activity as CSV."""
        params = req.query
        csv_data = self._store.export_csv(
            session_id=params.get("session_id", ""),
            tool=params.get("tool", ""),
            category=params.get("category", ""),
            status=params.get("status", ""),
            model=params.get("model", ""),
            interaction_type=params.get("interaction_type", ""),
            flagged_only=params.get("flagged", "").lower() in ("1", "true", "yes"),
        )
        return web.Response(
            body=csv_data,
            content_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=tool-activity.csv"},
        )

    async def _import(self, _req: web.Request) -> web.Response:
        """Backfill tool activity from existing session history."""
        count = self._store.import_from_sessions(self._sessions)
        return web.json_response({"status": "ok", "imported": count})
