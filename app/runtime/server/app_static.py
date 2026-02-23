"""Static / SPA file handlers and voice route delegation."""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from ..config.settings import cfg
from ..media import EXTENSION_TO_MIME

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

# -- Voice dynamic route handler factory -----------------------------------

_VOICE_NOT_CONFIGURED = {
    "status": "error",
    "message": (
        "Voice calling is not configured. Deploy ACS + "
        "Azure OpenAI resources in the Voice Call section first."
    ),
}


def voice_handler(
    method_name: str, *, log_label: str = "",
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Create a dynamic voice route handler that delegates to the app's handler."""

    async def handler(req: web.Request) -> web.Response:
        h = req.app.get("_voice_handler")
        if log_label:
            logger.info(
                "%s hit: method=%s path=%s handler=%s",
                log_label, req.method, req.path,
                "configured" if h else "NONE",
            )
        if h is None:
            return web.json_response(_VOICE_NOT_CONFIGURED, status=400)
        return await getattr(h, method_name)(req)

    return handler


# -- Static file handlers -------------------------------------------------


async def serve_media(req: web.Request) -> web.Response:
    """Serve an uploaded media file from the outgoing directory."""
    filename = req.match_info["filename"]
    if ".." in filename or filename.startswith("/"):
        return web.Response(status=403, text="Forbidden")
    file_path = cfg.media_outgoing_sent_dir / filename
    if not file_path.is_file():
        return web.Response(status=404, text="Not found")
    content_type = (
        EXTENSION_TO_MIME.get(file_path.suffix.lower())
        or mimetypes.guess_type(file_path.name)[0]
        or "application/octet-stream"
    )
    return web.FileResponse(file_path, headers={"Content-Type": content_type})


def make_file_handler(fpath: Path) -> Callable:
    """Return a handler that serves a single static file."""

    async def handler(_req: web.Request) -> web.Response:
        ct = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
        return web.FileResponse(fpath, headers={"Content-Type": ct})

    return handler


async def serve_index(req: web.Request) -> web.Response:
    """Serve the frontend index.html with no-cache headers."""
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        return web.Response(status=404, text="Not found")
    html = index.read_text()
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def serve_spa_or_404(req: web.Request) -> web.Response:
    """Serve the SPA for non-API paths, 404 for unknown /api/ paths."""
    if req.path.startswith("/api/"):
        raise web.HTTPNotFound(
            text='{"status":"error","message":"Unknown endpoint: '
            f'{req.method} {req.path}"' + '}',
            content_type="application/json",
        )
    return await serve_index(req)
