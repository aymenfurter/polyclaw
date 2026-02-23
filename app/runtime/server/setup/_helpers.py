"""Shared helpers for setup route handlers."""

from __future__ import annotations

from aiohttp import web


def ok_response(message: str) -> web.Response:
    """Return a standard success response."""
    return web.json_response({"status": "ok", "message": message})


def error_response(message: str, status: int = 500) -> web.Response:
    """Return a standard error response."""
    return web.json_response({"status": "error", "message": message}, status=status)
