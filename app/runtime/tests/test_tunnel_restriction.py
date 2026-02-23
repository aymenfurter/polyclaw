"""Tests for the tunnel restriction and auth middlewares via real HTTP calls.

We spin up a minimal aiohttp app with the middlewares and hit it with a
TestClient, verifying that:
  - allowed prefixes pass through when requests arrive via a tunnel
  - everything else gets a 403 when tunnel_restricted is on
  - non-tunnel requests are never blocked
  - the 403 body leaks no internal details
  - admin secret protects /api/* endpoints
  - public endpoints don't require auth
  - Bearer header, ?token=, ?secret= all grant access
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.runtime.config import cfg
from app.runtime.server.middleware import (
    _CF_HEADERS,
    _PUBLIC_EXACT,
    _PUBLIC_PREFIXES,
    _TUNNEL_ALLOWED_PREFIXES,
    auth_middleware,
    tunnel_restriction_middleware,
)


# -- helpers ----------------------------------------------------------------

async def _ok_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _build_app(middlewares=None) -> web.Application:
    """Minimal app with given middlewares and a catch-all route."""
    if middlewares is None:
        middlewares = [tunnel_restriction_middleware]
    app = web.Application(middlewares=middlewares)
    app.router.add_route("*", "/{path:.*}", _ok_handler)
    return app


def _build_tunnel_app() -> web.Application:
    return _build_app([tunnel_restriction_middleware])


def _build_auth_app() -> web.Application:
    return _build_app([auth_middleware])


def _build_full_app() -> web.Application:
    return _build_app([tunnel_restriction_middleware, auth_middleware])


_TUNNEL_HEADERS = {"cf-connecting-ip": "1.2.3.4"}

# paths that MUST be reachable through a tunnel in restricted mode
_ALLOWED_PATHS = [
    "/health",
    "/api/messages",
    "/api/messages/extra",
    "/acs",
    "/acs/callback",
    "/realtime-acs",
    "/realtime-acs/session",
    "/api/voice/acs-callback",
    "/api/voice/acs-callback/event",
    "/api/voice/media-streaming",
    "/api/voice/media-streaming/ws",
]

# paths that MUST be blocked through a tunnel in restricted mode
_BLOCKED_PATHS = [
    "/",
    "/api/config",
    "/api/schedules",
    "/api/plugins",
    "/api/setup/status",
    "/api/sandbox/config",
    "/api/foundry-iq/config",
    "/api/sessions",
    "/api/profile",
    "/api/network/info",
]


# -- tests ------------------------------------------------------------------


class TestTunnelRestrictionMiddleware:
    """Actual network-call tests against the middleware."""

    # -- restricted mode ON, request via tunnel --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _ALLOWED_PATHS)
    async def test_allowed_path_passes_through_tunnel(self, path: str) -> None:
        """Allowed prefixes must return 200 even via tunnel when restricted."""
        cfg.tunnel_restricted = True
        app = _build_tunnel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path, headers=_TUNNEL_HEADERS)
            assert resp.status == 200, f"Expected 200 for {path}, got {resp.status}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _BLOCKED_PATHS)
    async def test_blocked_path_returns_403_via_tunnel(self, path: str) -> None:
        """Non-allowed paths must be blocked with 403 via tunnel."""
        cfg.tunnel_restricted = True
        app = _build_tunnel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path, headers=_TUNNEL_HEADERS)
            assert resp.status == 403, f"Expected 403 for {path}, got {resp.status}"

    @pytest.mark.asyncio
    async def test_403_body_leaks_nothing(self) -> None:
        """The 403 response must not expose internal details."""
        cfg.tunnel_restricted = True
        app = _build_tunnel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/config", headers=_TUNNEL_HEADERS)
            assert resp.status == 403
            body = await resp.json()
            assert body == {"status": "forbidden"}
            assert "message" not in body
            assert "tunnel" not in str(body).lower()
            assert "restricted" not in str(body).lower()

    # -- restricted mode ON, request NOT through tunnel --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _BLOCKED_PATHS)
    async def test_non_tunnel_request_passes_when_restricted(self, path: str) -> None:
        """Direct (non-tunnel) requests must not be blocked regardless of path."""
        cfg.tunnel_restricted = True
        app = _build_tunnel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)  # no CF headers
            assert resp.status == 200, f"Expected 200 for direct {path}, got {resp.status}"

    # -- restricted mode OFF --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _BLOCKED_PATHS)
    async def test_everything_passes_when_not_restricted(self, path: str) -> None:
        """With tunnel_restricted=False, all paths must be accessible."""
        cfg.tunnel_restricted = False
        app = _build_tunnel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path, headers=_TUNNEL_HEADERS)
            assert resp.status == 200, f"Expected 200 for {path}, got {resp.status}"

    # -- CF header detection --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", list(_CF_HEADERS))
    async def test_each_cf_header_triggers_tunnel_detection(self, header: str) -> None:
        """Any single CF header must be enough to identify a tunnel request."""
        cfg.tunnel_restricted = True
        app = _build_tunnel_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/config", headers={header: "value"})
            assert resp.status == 403, f"CF header '{header}' did not trigger tunnel detection"

    # -- POST / PUT / DELETE --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["post", "put", "delete"])
    async def test_blocked_for_all_http_methods(self, method: str) -> None:
        """Blocked paths must be blocked regardless of HTTP method."""
        cfg.tunnel_restricted = True
        app = _build_tunnel_app()
        async with TestClient(TestServer(app)) as client:
            fn = getattr(client, method)
            resp = await fn("/api/config", headers=_TUNNEL_HEADERS)
            assert resp.status == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["post", "put"])
    async def test_allowed_for_all_http_methods(self, method: str) -> None:
        """Allowed paths must work for all HTTP methods."""
        cfg.tunnel_restricted = True
        app = _build_tunnel_app()
        async with TestClient(TestServer(app)) as client:
            fn = getattr(client, method)
            resp = await fn("/api/messages", headers=_TUNNEL_HEADERS)
            assert resp.status == 200

    # -- sanity: allowed prefixes match the constant --

    def test_allowed_paths_all_covered(self) -> None:
        """Every _TUNNEL_ALLOWED_PREFIXES entry has at least one test path."""
        for prefix in _TUNNEL_ALLOWED_PREFIXES:
            assert any(
                p.startswith(prefix) for p in _ALLOWED_PATHS
            ), f"Prefix '{prefix}' has no test path in _ALLOWED_PATHS"


# ---------------------------------------------------------------------------
# Auth middleware tests
# ---------------------------------------------------------------------------

_TEST_SECRET = "test-admin-secret-42"

# Paths that should NOT require auth (public)
_PUBLIC_PATHS = [
    "/health",
    "/api/messages",
    "/api/messages/extra",
    "/acs",
    "/acs/callback",
    "/realtime-acs",
    "/realtime-acs/session",
    "/api/voice/acs-callback",
    "/api/voice/acs-callback/event",
    "/api/voice/media-streaming",
    "/api/voice/media-streaming/ws",
    "/api/auth/check",
]

# Paths that MUST require auth
_PROTECTED_PATHS = [
    "/api/config",
    "/api/schedules",
    "/api/plugins",
    "/api/setup/status",
    "/api/sandbox/config",
    "/api/foundry-iq/config",
    "/api/sessions",
    "/api/profile",
    "/api/network/info",
    "/api/network/resource-audit",
]

# Non-API paths should never require auth
_NON_API_PATHS = [
    "/",
    "/index.html",
    "/assets/main.js",
    "/favicon.ico",
]


class TestAuthMiddleware:
    """Actual network-call tests against the auth middleware."""

    # -- no secret configured: everything passes --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _PROTECTED_PATHS)
    async def test_no_secret_configured_allows_all(self, path: str) -> None:
        """When admin_secret is empty, all endpoints are accessible."""
        cfg.admin_secret = ""
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)
            assert resp.status == 200, f"Expected 200 for {path} (no secret), got {resp.status}"

    # -- secret configured: protected paths blocked without auth --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _PROTECTED_PATHS)
    async def test_protected_path_returns_401_without_auth(self, path: str) -> None:
        """Protected endpoints must return 401 without valid auth."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)
            assert resp.status == 401, f"Expected 401 for {path}, got {resp.status}"

    @pytest.mark.asyncio
    async def test_401_body_content(self) -> None:
        """The 401 response must indicate unauthorized."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/config")
            assert resp.status == 401
            body = await resp.json()
            assert body["status"] == "unauthorized"

    # -- public paths pass without auth --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _PUBLIC_PATHS)
    async def test_public_path_no_auth_required(self, path: str) -> None:
        """Public endpoints must be accessible without auth."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)
            assert resp.status == 200, f"Expected 200 for public {path}, got {resp.status}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _NON_API_PATHS)
    async def test_non_api_path_no_auth_required(self, path: str) -> None:
        """Non-API paths (frontend assets, root) must not require auth."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)
            assert resp.status == 200, f"Expected 200 for non-API {path}, got {resp.status}"

    # -- valid auth methods --

    @pytest.mark.asyncio
    async def test_bearer_header_grants_access(self) -> None:
        """Bearer token in Authorization header must grant access."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/config",
                headers={"Authorization": f"Bearer {_TEST_SECRET}"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_token_query_param_grants_access(self) -> None:
        """?token= query parameter must grant access."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/api/config?token={_TEST_SECRET}")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_secret_query_param_grants_access(self) -> None:
        """?secret= query parameter must grant access."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/api/config?secret={_TEST_SECRET}")
            assert resp.status == 200

    # -- invalid auth --

    @pytest.mark.asyncio
    async def test_wrong_bearer_token_rejected(self) -> None:
        """Wrong Bearer token must be rejected."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/config",
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_wrong_query_token_rejected(self) -> None:
        """Wrong ?token= must be rejected."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/config?token=wrong-token")
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_empty_bearer_rejected(self) -> None:
        """Empty Bearer header must be rejected."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/config",
                headers={"Authorization": "Bearer "},
            )
            assert resp.status == 401

    # -- auth works for all HTTP methods --

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
    async def test_auth_required_for_all_methods(self, method: str) -> None:
        """Auth must be checked for all HTTP methods on protected paths."""
        cfg.admin_secret = _TEST_SECRET
        app = _build_auth_app()
        async with TestClient(TestServer(app)) as client:
            fn = getattr(client, method)
            # Without auth
            resp = await fn("/api/config")
            assert resp.status == 401, f"{method.upper()} without auth should be 401"
            # With auth
            resp = await fn("/api/config", headers={"Authorization": f"Bearer {_TEST_SECRET}"})
            assert resp.status == 200, f"{method.upper()} with auth should be 200"

    # -- sanity: public prefixes match the constant --

    def test_public_paths_cover_all_prefixes(self) -> None:
        """Every _PUBLIC_PREFIXES entry has at least one test path."""
        for prefix in _PUBLIC_PREFIXES:
            assert any(
                p.startswith(prefix) for p in _PUBLIC_PATHS
            ), f"Prefix '{prefix}' has no test path in _PUBLIC_PATHS"

    def test_public_exact_paths_covered(self) -> None:
        """Every _PUBLIC_EXACT entry is in the test list."""
        for path in _PUBLIC_EXACT:
            assert path in _PUBLIC_PATHS, f"Exact public path '{path}' not in _PUBLIC_PATHS"


# ---------------------------------------------------------------------------
# Combined middleware tests (tunnel + auth together)
# ---------------------------------------------------------------------------


class TestCombinedMiddlewares:
    """Test tunnel restriction AND auth working together."""

    @pytest.mark.asyncio
    async def test_tunnel_blocked_before_auth_checked(self) -> None:
        """Tunnel restriction must block even if valid auth is provided."""
        cfg.tunnel_restricted = True
        cfg.admin_secret = _TEST_SECRET
        app = _build_full_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/config",
                headers={**_TUNNEL_HEADERS, "Authorization": f"Bearer {_TEST_SECRET}"},
            )
            assert resp.status == 403  # tunnel blocks first

    @pytest.mark.asyncio
    async def test_allowed_tunnel_path_still_needs_no_auth(self) -> None:
        """Public paths via tunnel must pass both middlewares without auth."""
        cfg.tunnel_restricted = True
        cfg.admin_secret = _TEST_SECRET
        app = _build_full_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/messages", headers=_TUNNEL_HEADERS)
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_direct_protected_needs_auth(self) -> None:
        """Direct (non-tunnel) request to protected path still needs auth."""
        cfg.tunnel_restricted = True
        cfg.admin_secret = _TEST_SECRET
        app = _build_full_app()
        async with TestClient(TestServer(app)) as client:
            # No tunnel headers, no auth
            resp = await client.get("/api/config")
            assert resp.status == 401
            # No tunnel headers, with auth
            resp = await client.get(
                "/api/config",
                headers={"Authorization": f"Bearer {_TEST_SECRET}"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_restricted_still_needs_auth(self) -> None:
        """With tunnel unrestricted, auth is still required on protected paths."""
        cfg.tunnel_restricted = False
        cfg.admin_secret = _TEST_SECRET
        app = _build_full_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/config", headers=_TUNNEL_HEADERS)
            assert resp.status == 401
            resp = await client.get(
                "/api/config",
                headers={**_TUNNEL_HEADERS, "Authorization": f"Bearer {_TEST_SECRET}"},
            )
            assert resp.status == 200
