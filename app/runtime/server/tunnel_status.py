"""Resolve tunnel status from the bot service endpoint (admin-only mode)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ..services.cloud.azure import AzureCLI
from ..util.async_helpers import run_sync
from ..util.singletons import register_singleton

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=5)

# Cache the bot endpoint URL for 60 s (az bot show is slow).
_ENDPOINT_CACHE_TTL = 60.0
# Cache the probe result for 15 s (health check is fast but frequent).
_PROBE_CACHE_TTL = 15.0


@dataclass
class _TunnelCache:
    """Mutable cache for tunnel endpoint and probe results."""

    endpoint: str | None = None
    endpoint_ts: float = 0.0
    probe: bool = False
    probe_url: str | None = None
    probe_ts: float = 0.0


_cache = _TunnelCache()


def _reset_tunnel_cache() -> None:
    """Reset tunnel cache to default values (for test isolation)."""
    global _cache  # noqa: PLW0603
    _cache = _TunnelCache()


register_singleton(_reset_tunnel_cache)


async def resolve_tunnel_info(
    tunnel: object | None,
    az: AzureCLI | None,
) -> dict[str, Any]:
    """Return tunnel status dict suitable for API responses.

    When the local ``CloudflareTunnel`` instance is active (runtime / combined
    mode), use its state directly.  Otherwise (admin-only mode), read the
    messaging endpoint from the deployed Azure Bot Service and probe the
    tunnel's ``/health`` endpoint to determine reachability.
    """
    # Fast path: local tunnel object is available and active
    if tunnel is not None and getattr(tunnel, "is_active", False):
        return {
            "active": True,
            "url": getattr(tunnel, "url", None),
            "restricted": _restricted(),
        }

    # Admin-only path: read endpoint from bot service, probe it
    if az and _bot_configured():
        endpoint = await _get_bot_endpoint_cached(az)
        if endpoint:
            tunnel_url = _endpoint_to_tunnel_url(endpoint)
            active = await _probe_tunnel_cached(tunnel_url)
            return {
                "active": active,
                "url": tunnel_url if active else None,
                "restricted": _restricted(),
            }

    # Fallback: no tunnel info available
    return {
        "active": False,
        "url": None,
        "restricted": _restricted(),
    }


def _restricted() -> bool:
    from ..config.settings import cfg

    return cfg.tunnel_restricted


def _bot_configured() -> bool:
    from ..config.settings import cfg

    return bool(cfg.env.read("BOT_NAME"))


def _endpoint_to_tunnel_url(endpoint: str) -> str:
    """Strip ``/api/messages`` suffix to get the base tunnel URL."""
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/api/messages"):
        return endpoint[: -len("/api/messages")]
    return endpoint


async def _get_bot_endpoint_cached(az: AzureCLI) -> str | None:
    """Return the bot messaging endpoint, cached for ``_ENDPOINT_CACHE_TTL`` s."""
    now = time.monotonic()
    if _cache.endpoint is not None and (now - _cache.endpoint_ts) < _ENDPOINT_CACHE_TTL:
        return _cache.endpoint

    endpoint = await run_sync(az.get_bot_endpoint)
    _cache.endpoint = endpoint
    _cache.endpoint_ts = now
    return endpoint


async def _probe_tunnel_cached(url: str) -> bool:
    """Probe the tunnel with a short TTL cache to avoid hammering."""
    now = time.monotonic()
    if (
        _cache.probe_url == url
        and (now - _cache.probe_ts) < _PROBE_CACHE_TTL
    ):
        return _cache.probe

    active = await _probe_tunnel(url)
    _cache.probe = active
    _cache.probe_url = url
    _cache.probe_ts = now
    return active


async def _probe_tunnel(url: str) -> bool:
    """Probe the tunnel's /health endpoint. Active if it responds without error."""
    health_url = url.rstrip("/") + "/health"
    try:
        async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as session:
            async with session.get(health_url) as resp:
                return resp.status < 500
    except Exception:
        logger.debug("[tunnel_status] probe failed for %s", health_url, exc_info=True)
        return False

