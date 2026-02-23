"""HTTP middleware -- auth, lockdown, and tunnel restrictions."""

from __future__ import annotations

import hmac
import logging

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from ..config.settings import cfg

logger = logging.getLogger(__name__)

_QUIET_PATHS = frozenset({"/api/setup/status", "/health"})


class QuietAccessLogger(AbstractAccessLogger):
    """Demotes polling-endpoint and noisy log entries to DEBUG."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        status = response.status
        if request.path in _QUIET_PATHS or status == 401 or status in (502, 503):
            level = logging.DEBUG
        else:
            level = logging.INFO
        self.logger.log(
            level,
            "%s %s %s %s %.3fs",
            request.remote,
            request.method,
            request.path,
            status,
            time,
        )


_PUBLIC_PREFIXES = (
    "/health",
    "/api/messages",
    "/acs",
    "/realtime-acs",
    "/api/voice/acs-callback",
    "/api/voice/media-streaming",
)
_PUBLIC_EXACT = ("/api/auth/check",)

# Tunnel restrictions and lockdown share the same base set as public prefixes;
# lockdown adds one extra path.
_TUNNEL_ALLOWED_PREFIXES = _PUBLIC_PREFIXES
_LOCKDOWN_ALLOWED_PREFIXES = _PUBLIC_PREFIXES + ("/api/setup/lockdown",)

_CF_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-ipcountry")


@web.middleware
async def lockdown_middleware(request: web.Request, handler):  # type: ignore[type-arg]
    """Block all admin panel routes when lockdown mode is active."""
    if not cfg.lockdown_mode:
        return await handler(request)
    if any(request.path.startswith(p) for p in _LOCKDOWN_ALLOWED_PREFIXES):
        return await handler(request)
    return web.json_response(
        {
            "status": "locked",
            "message": (
                "Lock Down Mode is active. The admin panel is disabled. "
                "Use /lockdown off via the bot to restore access."
            ),
        },
        status=403,
    )


@web.middleware
async def tunnel_restriction_middleware(request: web.Request, handler):  # type: ignore[type-arg]
    """Restrict Cloudflare-tunnelled requests to bot-only endpoints."""
    if not cfg.tunnel_restricted:
        return await handler(request)
    is_tunnel = any(request.headers.get(h) for h in _CF_HEADERS)
    if not is_tunnel:
        return await handler(request)
    if any(request.path.startswith(p) for p in _TUNNEL_ALLOWED_PREFIXES):
        return await handler(request)
    return web.json_response({"status": "forbidden"}, status=403)


@web.middleware
async def auth_middleware(request: web.Request, handler):  # type: ignore[type-arg]
    """Require Bearer token on ``/api/*`` endpoints (except public ones)."""
    secret = cfg.admin_secret
    if not secret:
        return await handler(request)

    path = request.path

    # Only protect /api/* endpoints (except public ones); frontend assets are public
    if not path.startswith("/api/"):
        return await handler(request)

    if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await handler(request)

    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    if hmac.compare_digest(auth, expected):
        return await handler(request)

    token_param = request.query.get("token", "")
    if token_param and hmac.compare_digest(token_param, secret):
        return await handler(request)

    secret_param = request.query.get("secret", "")
    if secret_param and hmac.compare_digest(secret_param, secret):
        return await handler(request)

    return web.json_response(
        {"status": "unauthorized", "message": "Invalid or missing admin secret"},
        status=401,
    )
