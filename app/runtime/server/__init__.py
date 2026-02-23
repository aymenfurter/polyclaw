"""Server module -- aiohttp application factory and HTTP/WS handlers."""

from __future__ import annotations

from .app import AppFactory, create_app, main
from .wiring import create_adapter

__all__ = ["AppFactory", "create_adapter", "create_app", "main"]
