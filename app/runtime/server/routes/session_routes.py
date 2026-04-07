"""Session history API routes -- /api/sessions/*."""

from __future__ import annotations

from aiohttp import web

from ...state.session_store import ARCHIVAL_OPTIONS, SessionStore
from ._helpers import error_response, ok_response, parse_json


class SessionRoutes:
    """REST handler for chat session history."""

    def __init__(self, session_store: SessionStore) -> None:
        self._store = session_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/sessions", self._list)
        router.add_get("/api/sessions/stats", self._stats)
        router.add_get("/api/sessions/policy", self._get_policy)
        router.add_put("/api/sessions/policy", self._set_policy)
        router.add_get("/api/sessions/{session_id}", self._get)
        router.add_delete("/api/sessions/{session_id}", self._delete)
        router.add_delete("/api/sessions", self._clear)

    async def _list(self, _req: web.Request) -> web.Response:
        return web.json_response(self._store.list_sessions())

    async def _get(self, req: web.Request) -> web.Response:
        session_id = req.match_info["session_id"]
        data = self._store.get_session(session_id)
        if not data:
            return error_response("Session not found", status=404)
        return web.json_response(data)

    async def _delete(self, req: web.Request) -> web.Response:
        session_id = req.match_info["session_id"]
        removed = self._store.delete_session(session_id)
        if not removed:
            return error_response("Session not found", status=404)
        return ok_response()

    async def _clear(self, _req: web.Request) -> web.Response:
        count = self._store.clear_all()
        return ok_response(deleted=count)

    async def _stats(self, _req: web.Request) -> web.Response:
        return web.json_response(self._store.get_session_stats())

    async def _get_policy(self, _req: web.Request) -> web.Response:
        return web.json_response({
            "policy": self._store.get_archival_policy(),
            "options": list(ARCHIVAL_OPTIONS.keys()),
        })

    async def _set_policy(self, req: web.Request) -> web.Response:
        body = await parse_json(req)
        policy = body.get("policy", "")
        if policy not in ARCHIVAL_OPTIONS:
            return error_response(
                f"Invalid policy. Valid: {list(ARCHIVAL_OPTIONS.keys())}",
            )
        self._store.set_archival_policy(policy)
        stats = self._store.get_session_stats()
        return web.json_response({"status": "ok", "policy": policy, **stats})
