"""Shared helpers for route handlers."""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# -- response helpers ------------------------------------------------------


def ok_response(**data: Any) -> web.Response:
    """Return a standard ``{"status": "ok", ...}`` JSON response."""
    return web.json_response({"status": "ok", **data})


def error_response(message: str, *, status: int = 400) -> web.Response:
    """Return a standard ``{"status": "error", "message": ...}`` JSON response."""
    return web.json_response({"status": "error", "message": message}, status=status)


def no_az() -> web.Response:
    """Return a standard error when Azure CLI is unavailable."""
    return error_response("Azure CLI not available", status=500)


def fail_response(steps: list[dict[str, Any]]) -> web.Response:
    """Return a standard provisioning-failure response with step details."""
    failed = [s for s in steps if s.get("status") == "failed"]
    msg = failed[0].get("detail", "Unknown error") if failed else "Unknown error"
    return web.json_response(
        {"status": "error", "steps": steps, "message": f"Provisioning failed: {msg}"},
        status=500,
    )


# -- request helpers -------------------------------------------------------


async def parse_json(req: web.Request) -> dict[str, Any]:
    """Parse JSON body, raising ``ValueError`` on invalid input."""
    try:
        body = await req.json()
    except (json.JSONDecodeError, Exception) as exc:
        raise ValueError("Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


# -- decorator -------------------------------------------------------------

Handler = Callable[..., Awaitable[web.Response]]


def api_handler(fn: Handler) -> Handler:
    """Wrap a route handler with standard JSON error handling.

    Catches ``ValueError`` (→ 400) and unexpected exceptions (→ 500),
    returning the standard error envelope.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> web.Response:
        try:
            return await fn(*args, **kwargs)
        except ValueError as exc:
            return error_response(str(exc))
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.exception("Unhandled error in %s: %s", fn.__qualname__, exc)
            return error_response("Internal server error", status=500)

    return wrapper
