"""Reverse proxy -- forwards unmatched /api/* requests to the runtime container."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

_RUNTIME_URL = os.getenv("RUNTIME_URL", "http://runtime:8080")

# Hop-by-hop headers that must not be forwarded verbatim.
_HOP_BY_HOP = frozenset({
    "host",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
    "te",
    "trailers",
})


def _forward_headers(raw: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop headers before proxying."""
    return {k: v for k, v in raw.items() if k.lower() not in _HOP_BY_HOP}


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------


async def _proxy_http(
    request: web.Request,
    target_url: str,
    session: aiohttp.ClientSession,
) -> web.Response:
    headers = _forward_headers(dict(request.headers))
    body = await request.read()

    try:
        async with session.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=body if body else None,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            response_body = await resp.read()
            resp_headers = {
                k: v
                for k, v in resp.headers.items()
                if k.lower() not in {"transfer-encoding", "content-encoding", "content-length"}
            }
            return web.Response(
                status=resp.status,
                body=response_body,
                headers=resp_headers,
            )
    except (aiohttp.ClientConnectorError, aiohttp.ClientOSError, OSError):
        logger.debug("[proxy.http] runtime unreachable: %s", target_url)
        raise web.HTTPBadGateway(text="Runtime container unreachable")
    except Exception:
        logger.warning("[proxy.http] runtime proxy error: %s", target_url, exc_info=True)
        raise web.HTTPBadGateway(text="Runtime container unreachable")


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------


async def _proxy_websocket(
    request: web.Request,
    target_url: str,
) -> web.WebSocketResponse:
    ws_target = target_url.replace("http://", "ws://").replace("https://", "wss://")

    proxy_headers: dict[str, str] = {}
    auth = request.headers.get("Authorization")
    if auth:
        proxy_headers["Authorization"] = auth

    # Connect to the runtime FIRST.  Only if that succeeds do we complete
    # the WebSocket handshake with the browser.  This avoids the
    # "Connected → Disconnected" flash when the runtime is still starting.
    session = aiohttp.ClientSession()
    try:
        ws_client = await session.ws_connect(ws_target, headers=proxy_headers)
    except Exception:
        await session.close()
        logger.debug("[proxy.ws] runtime not ready: %s", ws_target)
        raise web.HTTPServiceUnavailable(text="Runtime container not ready")

    ws_server = web.WebSocketResponse()
    await ws_server.prepare(request)

    try:

        async def _runtime_to_client() -> None:
            async for msg in ws_client:
                if ws_server.closed:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await ws_server.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await ws_server.send_bytes(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        async def _client_to_runtime() -> None:
            async for msg in ws_server:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await ws_client.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await ws_client.send_bytes(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        await asyncio.gather(
            _runtime_to_client(),
            _client_to_runtime(),
            return_exceptions=True,
        )
    except Exception:
        logger.warning("[proxy.ws] WS bridge error: %s", ws_target, exc_info=True)
    finally:
        if not ws_client.closed:
            await ws_client.close()
        await session.close()
        if not ws_server.closed:
            await ws_server.close()

    return ws_server


# ---------------------------------------------------------------------------
# Middleware factory
# ---------------------------------------------------------------------------


def create_runtime_proxy_middleware(
    runtime_url: str | None = None,
) -> Callable:
    """Create an aiohttp middleware that proxies unmatched ``/api/*``
    requests to the runtime container.

    Used in admin-only mode so the SPA served from the admin container
    can transparently reach runtime routes (chat, skills, profile, etc.).

    The proxy target URL is determined at startup from ``RUNTIME_URL``
    (defaults to ``http://runtime:8080``).
    """
    initial_base = (runtime_url or _RUNTIME_URL).rstrip("/")
    session: aiohttp.ClientSession | None = None

    def _resolve_base() -> str:
        """Return the current runtime base URL.

        After an ACA deploy, the deployer updates ``os.environ["RUNTIME_URL"]``
        so the proxy automatically routes to the new ACA FQDN without a
        restart.
        """
        return os.getenv("RUNTIME_URL", initial_base).rstrip("/")

    async def _get_session() -> aiohttp.ClientSession:
        nonlocal session
        if session is None or session.closed:
            session = aiohttp.ClientSession()
        return session

    @web.middleware
    async def runtime_proxy_middleware(
        request: web.Request,
        handler: Callable,
    ) -> web.StreamResponse:
        try:
            resp = await handler(request)
        except (web.HTTPNotFound, web.HTTPMethodNotAllowed):
            if not request.path.startswith("/api/"):
                raise

            base = _resolve_base()

            # Build the target URL on the runtime container
            target = f"{base}{request.path}"
            if request.query_string:
                target += f"?{request.query_string}"

            logger.debug("[proxy] forwarding %s %s -> %s", request.method, request.path, target)

            # WebSocket upgrade
            if (
                request.headers.get("Upgrade", "").lower() == "websocket"
                or request.headers.get("Connection", "").lower() == "upgrade"
            ):
                return await _proxy_websocket(request, target)

            # Regular HTTP
            return await _proxy_http(request, target, await _get_session())

        return resp

    async def cleanup(_app: web.Application) -> None:
        nonlocal session
        if session and not session.closed:
            await session.close()
            session = None

    # Attach cleanup so the caller can register it on app shutdown
    runtime_proxy_middleware.cleanup = cleanup  # type: ignore[attr-defined]
    return runtime_proxy_middleware
