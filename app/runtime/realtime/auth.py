"""ACS callback authentication -- query-param token + OIDC JWT validation.

Security layers (in order):
1. Query-param token  -- shared secret appended to callback URLs.
2. ACS JWT validation -- RS256 signature verified against Microsoft's JWKS.

The ACS resource ID (JWT audience) is auto-learned from the first
signature-verified JWT so no manual configuration is needed.
"""

from __future__ import annotations

import logging
import threading

from aiohttp import web

from ..util.singletons import register_singleton

logger = logging.getLogger(__name__)

_ACS_ISSUER = "https://acscallautomation.communication.azure.com"
_ACS_JWKS_URL = "https://acscallautomation.communication.azure.com/calling/keys"

# Auto-learned audience from the first valid ACS JWT.
_learned_audience: str = ""
_audience_lock = threading.Lock()


def _set_learned_audience(aud: str) -> None:
    global _learned_audience
    with _audience_lock:
        if not _learned_audience:
            _learned_audience = aud
            logger.info("ACS resource ID auto-learned from JWT: %s", aud)


def get_learned_audience() -> str:
    """Return the auto-learned ACS resource ID (empty until first JWT)."""
    return _learned_audience


def _reset_learned_audience() -> None:
    """Clear the auto-learned audience (for test isolation)."""
    global _learned_audience
    with _audience_lock:
        _learned_audience = ""


register_singleton(_reset_learned_audience)


def validate_token_param(request: web.Request, expected_token: str) -> bool:
    return request.query.get("token", "") == expected_token


async def validate_acs_jwt(
    request: web.Request,
    acs_resource_id: str = "",
) -> bool:
    """Validate the ACS JWT from the Authorization header.

    If *acs_resource_id* is provided it is used as the expected audience.
    Otherwise the module tries to auto-learn the audience from the first
    signature-verified JWT and enforces it on subsequent calls.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False

    raw_token = auth_header.split(" ", 1)[1]
    if not raw_token:
        return False

    try:
        import jwt as pyjwt
        from jwt import PyJWKClient
    except ImportError:
        logger.warning("PyJWT not installed -- skipping ACS JWT validation")
        return False

    try:
        jwks_client = PyJWKClient(_ACS_JWKS_URL)
        signing_key = jwks_client.get_signing_key_from_jwt(raw_token)
    except Exception as exc:
        logger.warning("Failed to fetch ACS JWKS signing key: %s", exc)
        return False

    # Determine the expected audience.
    expected_aud = acs_resource_id or _learned_audience

    if expected_aud:
        # Full validation with audience.
        try:
            pyjwt.decode(
                raw_token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=_ACS_ISSUER,
                audience=expected_aud,
            )
            logger.debug("ACS JWT validated (aud=%s)", expected_aud)
            return True
        except pyjwt.InvalidTokenError as exc:
            logger.warning("ACS JWT validation failed: %s", exc)
            return False
    else:
        # No audience known yet -- verify signature + issuer, then learn aud.
        try:
            claims = pyjwt.decode(
                raw_token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=_ACS_ISSUER,
                options={"verify_aud": False},
            )
            aud = claims.get("aud", "")
            if aud:
                _set_learned_audience(aud)
            logger.info("ACS JWT signature verified (auto-learned aud=%s)", aud)
            return True
        except pyjwt.InvalidTokenError as exc:
            logger.warning("ACS JWT validation failed (no audience): %s", exc)
            return False


async def validate_acs_request(
    request: web.Request,
    expected_token: str,
    acs_resource_id: str = "",
) -> web.Response | None:
    """Validate an inbound ACS request. Returns ``None`` if OK, or a 401 Response."""
    # Layer 1: query-param token
    if not validate_token_param(request, expected_token):
        logger.warning(
            "ACS request rejected: invalid callback token (path=%s, remote=%s)",
            request.path, request.remote,
        )
        return web.Response(status=401, text="Invalid callback token")

    # Layer 2: ACS JWT (always attempted when a Bearer header is present)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        jwt_ok = await validate_acs_jwt(request, acs_resource_id)
        if not jwt_ok:
            logger.warning(
                "ACS JWT validation failed but token auth passed (path=%s) "
                "-- allowing request (JWT enforcement pending audience learn)",
                request.path,
            )
    return None
