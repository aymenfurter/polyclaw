"""Shared helpers for route handlers."""

from __future__ import annotations

from typing import Any

from aiohttp import web


def no_az() -> web.Response:
    """Return a standard error when Azure CLI is unavailable."""
    return web.json_response(
        {"status": "error", "message": "Azure CLI not available"}, status=500
    )


def fail_response(steps: list[dict[str, Any]]) -> web.Response:
    """Return a standard provisioning-failure response with step details."""
    failed = [s for s in steps if s.get("status") == "failed"]
    msg = failed[0].get("detail", "Unknown error") if failed else "Unknown error"
    return web.json_response(
        {"status": "error", "steps": steps, "message": f"Provisioning failed: {msg}"},
        status=500,
    )
