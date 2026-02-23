"""MCP (Model Context Protocol) server management API routes -- /api/mcp/*."""

from __future__ import annotations

import logging

import aiohttp as _aiohttp
from aiohttp import web

from ...state.mcp_config import McpConfigStore

logger = logging.getLogger(__name__)

_GITHUB_MCP_REGISTRY_URL = "https://github.com/mcp"


class McpRoutes:
    """REST handler for MCP server configuration."""

    def __init__(self, config_store: McpConfigStore) -> None:
        self._store = config_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/mcp/servers", self._list)
        router.add_get("/api/mcp/servers/{server_id}", self._get)
        router.add_post("/api/mcp/servers", self._add)
        router.add_put("/api/mcp/servers/{server_id}", self._update)
        router.add_post("/api/mcp/servers/{server_id}/enable", self._enable)
        router.add_post("/api/mcp/servers/{server_id}/disable", self._disable)
        router.add_delete("/api/mcp/servers/{server_id}", self._remove)
        router.add_get("/api/mcp/registry", self._registry)

    async def _list(self, _req: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "servers": self._store.list_servers()})

    async def _get(self, req: web.Request) -> web.Response:
        server_id = req.match_info["server_id"]
        server = self._store.get_server(server_id)
        if not server:
            return web.json_response(
                {"status": "error", "message": "Server not found"}, status=404
            )
        return web.json_response(server)

    async def _add(self, req: web.Request) -> web.Response:
        data = await req.json()
        try:
            server = self._store.add_server(
                name=data.get("name", ""),
                server_type=data.get("type", ""),
                command=data.get("command", ""),
                args=data.get("args"),
                env=data.get("env"),
                url=data.get("url", ""),
                tools=data.get("tools"),
                enabled=data.get("enabled", True),
                description=data.get("description", ""),
            )
            return web.json_response({"status": "ok", "server": server})
        except (ValueError, KeyError) as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )

    async def _update(self, req: web.Request) -> web.Response:
        server_id = req.match_info["server_id"]
        data = await req.json()
        updated = self._store.update_server(server_id, **data)
        if not updated:
            return web.json_response(
                {"status": "error", "message": "Server not found"}, status=404
            )
        return web.json_response({"status": "ok", "server": updated})

    async def _enable(self, req: web.Request) -> web.Response:
        server_id = req.match_info["server_id"]
        ok = self._store.set_enabled(server_id, True)
        if not ok:
            return web.json_response(
                {"status": "error", "message": "Server not found"}, status=404
            )
        return web.json_response({"status": "ok"})

    async def _disable(self, req: web.Request) -> web.Response:
        server_id = req.match_info["server_id"]
        ok = self._store.set_enabled(server_id, False)
        if not ok:
            return web.json_response(
                {"status": "error", "message": "Server not found"}, status=404
            )
        return web.json_response({"status": "ok"})

    async def _remove(self, req: web.Request) -> web.Response:
        server_id = req.match_info["server_id"]
        ok = self._store.remove_server(server_id)
        if not ok:
            return web.json_response(
                {"status": "error", "message": "Server not found"}, status=404
            )
        return web.json_response({"status": "ok"})

    async def _registry(self, req: web.Request) -> web.Response:
        page = req.query.get("page", "1")
        query = req.query.get("q", "") or req.query.get("search", "")

        url = _GITHUB_MCP_REGISTRY_URL
        params: dict[str, str] = {"page": page}
        if query:
            params["q"] = query

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-requested-with": "XMLHttpRequest",
        }

        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=_aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {"status": "error", "message": f"GitHub registry returned HTTP {resp.status}"},
                            status=502,
                        )
                    data = await resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch GitHub MCP registry: %s", exc)
            return web.json_response(
                {"status": "error", "message": f"Failed to fetch registry: {exc}"},
                status=502,
            )

        # Extract server list from the nested payload
        payload = data.get("payload", {})
        route = payload.get("mcpRegistryRoute", {})
        servers_data = route.get("serversData", {})
        raw_servers = servers_data.get("servers", [])

        # Normalise into a cleaner shape for the frontend
        servers = []
        for srv in raw_servers:
            servers.append({
                "id": srv.get("id", ""),
                "name": srv.get("display_name") or srv.get("name", ""),
                "full_name": srv.get("name_with_owner") or srv.get("name", ""),
                "description": srv.get("description", ""),
                "url": srv.get("url", ""),
                "stars": srv.get("stargazer_count", 0),
                "language": srv.get("primary_language", ""),
                "language_color": srv.get("primary_language_color", ""),
                "license": srv.get("license", ""),
                "topics": srv.get("topics", []),
                "avatar_url": srv.get("owner_avatar_url", ""),
                "updated_at": srv.get("updated_at", ""),
                "pushed_at": srv.get("pushed_at", ""),
                "source": "github.com/mcp",
            })

        return web.json_response({
            "status": "ok",
            "page": int(page),
            "servers": servers,
            "source": "github.com/mcp",
        })
