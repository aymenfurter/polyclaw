"""Azure authentication and subscription routes -- /api/setup/azure/*."""

from __future__ import annotations

import logging

from aiohttp import web

from ...services.cloud.azure import AzureCLI
from ._helpers import error_response as _error, ok_response as _ok

logger = logging.getLogger(__name__)


class AzureSetupRoutes:
    """Handles Azure CLI login, logout, subscription listing."""

    def __init__(self, az: AzureCLI) -> None:
        self._az = az

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_post("/api/setup/azure/login", self.azure_login)
        router.add_get("/api/setup/azure/check", self.azure_check)
        router.add_post("/api/setup/azure/logout", self.azure_logout)
        router.add_get("/api/setup/azure/subscriptions", self.list_subscriptions)
        router.add_post("/api/setup/azure/subscription", self.set_subscription)
        router.add_get("/api/setup/azure/resource-groups", self.list_resource_groups)

    async def azure_login(self, _req: web.Request) -> web.Response:
        account = self._az.account_info()
        if account:
            return web.json_response({
                "status": "already_logged_in",
                "user": account.get("user", {}).get("name"),
                "subscription": account.get("name"),
            })
        info = self._az.login_device_code()
        return web.json_response({"status": "device_code_pending", **info})

    async def azure_check(self, _req: web.Request) -> web.Response:
        account = self._az.account_info()
        if account:
            return web.json_response({
                "status": "logged_in",
                "user": account.get("user", {}).get("name"),
                "subscription": account.get("name"),
            })
        return web.json_response({"status": "pending"})

    async def azure_logout(self, _req: web.Request) -> web.Response:
        ok, msg = self._az.ok("logout")
        self._az.invalidate_cache("account", "show")
        return _ok(msg) if ok else _error(msg)

    async def list_subscriptions(self, _req: web.Request) -> web.Response:
        subs = self._az.json("account", "list") or []
        return web.json_response([
            {
                "id": s.get("id", ""),
                "name": s.get("name", ""),
                "is_default": s.get("isDefault", False),
                "state": s.get("state", ""),
            }
            for s in (subs if isinstance(subs, list) else [])
        ])

    async def set_subscription(self, req: web.Request) -> web.Response:
        body = await req.json()
        sub_id = body.get("subscription_id", "").strip()
        if not sub_id:
            return _error("subscription_id is required", 400)
        ok, msg = self._az.ok("account", "set", "--subscription", sub_id)
        self._az.invalidate_cache("account", "show")
        return _ok(f"Subscription set to {sub_id}") if ok else _error(f"Failed: {msg}")

    async def list_resource_groups(self, _req: web.Request) -> web.Response:
        groups = self._az.json("group", "list") or []
        return web.json_response([
            {"name": g["name"], "location": g["location"]}
            for g in (groups if isinstance(groups, list) else [])
        ])
