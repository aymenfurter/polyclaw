"""Agent profile API routes -- /api/profile."""

from __future__ import annotations

from aiohttp import web

from ...state.profile import get_full_profile, load_profile, save_profile
from ._helpers import ok_response, parse_json


class ProfileRoutes:
    """REST handler for the agent profile."""

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/profile", self._get)
        router.add_post("/api/profile", self._update)

    async def _get(self, _req: web.Request) -> web.Response:
        return web.json_response(get_full_profile())

    async def _update(self, req: web.Request) -> web.Response:
        data = await parse_json(req)
        current = load_profile()
        for key in ("name", "emoji", "location", "emotional_state"):
            if key in data:
                current[key] = data[key]
        if "preferences" in data and isinstance(data["preferences"], dict):
            current["preferences"].update(data["preferences"])
        save_profile(current)
        return ok_response(message="Profile updated")
