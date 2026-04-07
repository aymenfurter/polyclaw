"""Shared helpers for setup route handlers."""

from __future__ import annotations

from typing import Any

from aiohttp import web


def ok_response(message: str) -> web.Response:
    """Return a standard success response."""
    return web.json_response({"status": "ok", "message": message})


def error_response(message: str, status: int = 500) -> web.Response:
    """Return a standard error response."""
    return web.json_response({"status": "error", "message": message}, status=status)


def fail_response(
    steps: list[dict[str, Any]],
    prefix: str = "Failed",
    *,
    key: str = "detail",
    status: int = 500,
) -> web.Response:
    """Return an error response with the first failed step's detail."""
    failed = [s for s in steps if s.get("status") == "failed"]
    msg = failed[0].get(key, "Unknown") if failed else "Unknown"
    return web.json_response(
        {"status": "error", "steps": steps, "message": f"{prefix}: {msg}"},
        status=status,
    )
