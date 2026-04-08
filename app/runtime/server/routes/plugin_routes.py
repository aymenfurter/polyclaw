"""Plugin management API routes -- /api/plugins/*."""

from __future__ import annotations

import io
import json
import logging
import zipfile

from aiohttp import web

from ...config.settings import cfg
from ...registries.plugins import PluginRegistry
from ...state.plugin_config import PluginConfigStore
from ._helpers import error_response, ok_response

logger = logging.getLogger(__name__)


class PluginRoutes:
    """REST handler for plugin management."""

    def __init__(
        self,
        registry: PluginRegistry,
        config_store: PluginConfigStore,
    ) -> None:
        self._registry = registry
        self._config = config_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/plugins", self._list)
        router.add_get("/api/plugins/{plugin_id}", self._get)
        router.add_post("/api/plugins/{plugin_id}/enable", self._enable)
        router.add_post("/api/plugins/{plugin_id}/disable", self._disable)
        router.add_get("/api/plugins/{plugin_id}/setup", self._setup_content)
        router.add_post("/api/plugins/{plugin_id}/setup", self._complete_setup)
        router.add_post("/api/plugins/import", self._import_zip)
        router.add_delete("/api/plugins/{plugin_id}", self._remove)

    async def _list(self, _req: web.Request) -> web.Response:
        return ok_response(plugins=self._registry.list_plugins())

    async def _get(self, req: web.Request) -> web.Response:
        plugin_id = req.match_info["plugin_id"]
        plugin = self._registry.get_plugin(plugin_id)
        if not plugin:
            return error_response("Plugin not found", status=404)
        return web.json_response(plugin)

    async def _enable(self, req: web.Request) -> web.Response:
        plugin_id = req.match_info["plugin_id"]
        result = self._registry.enable_plugin(plugin_id)
        if not result:
            return error_response("Plugin not found", status=404)
        return ok_response(message=f"Plugin '{result['name']}' enabled", plugin=result)

    async def _disable(self, req: web.Request) -> web.Response:
        plugin_id = req.match_info["plugin_id"]
        result = self._registry.disable_plugin(plugin_id)
        if not result:
            return error_response("Plugin not found", status=404)
        return ok_response(message=f"Plugin '{result['name']}' disabled", plugin=result)

    async def _setup_content(self, req: web.Request) -> web.Response:
        plugin_id = req.match_info["plugin_id"]
        manifest = self._registry.get_manifest(plugin_id)
        if not manifest:
            return error_response("Plugin not found", status=404)
        setup_md = manifest.setup_message or "No setup instructions available."
        return ok_response(content=setup_md)

    async def _complete_setup(self, req: web.Request) -> web.Response:
        plugin_id = req.match_info["plugin_id"]
        if not self._registry.get_manifest(plugin_id):
            return error_response("Plugin not found", status=404)
        self._config.mark_setup_completed(plugin_id)
        return ok_response()

    async def _import_zip(self, req: web.Request) -> web.Response:
        data = await req.read()
        if not data:
            return error_response("Empty body")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                manifest_name = next(
                    (n for n in names if n.endswith("manifest.json")), None
                )
                if not manifest_name:
                    return error_response("No manifest.json found")
                manifest_data = json.loads(zf.read(manifest_name))
                plugin_id = manifest_data.get("id", "")
                if not plugin_id:
                    return error_response("manifest.json missing 'id'")
                dest = cfg.plugins_dir / plugin_id
                dest.mkdir(parents=True, exist_ok=True)
                zf.extractall(dest)
        except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
            return error_response(f"Invalid plugin archive: {exc}")

        self._registry.refresh()
        return ok_response(plugin_id=plugin_id)

    async def _remove(self, req: web.Request) -> web.Response:
        plugin_id = req.match_info["plugin_id"]
        if not self._registry.get_manifest(plugin_id):
            return error_response("Plugin not found", status=404)
        self._config.reset(plugin_id)
        return ok_response()
