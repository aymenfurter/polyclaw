"""Download incoming attachments and build media-aware prompts."""

from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from pathlib import Path

from botbuilder.schema import Attachment

from ..config.settings import cfg
from ..util.async_helpers import run_sync
from .classify import classify

logger = logging.getLogger(__name__)


def _sync_download(url: str, dest: str) -> None:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "polyclaw/4.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        Path(dest).write_bytes(resp.read())


async def download_attachment(attachment: Attachment, channel_id: str) -> dict | None:
    cfg.ensure_dirs()

    url = attachment.content_url
    if not url:
        return None

    name = attachment.name or f"attachment_{uuid.uuid4().hex[:8]}"
    ext = os.path.splitext(name)[1]
    if not ext and attachment.content_type:
        ext = mimetypes.guess_extension(attachment.content_type.split(";")[0].strip()) or ""

    unique_name = f"{uuid.uuid4().hex[:8]}_{name}"
    local_path = cfg.media_incoming_dir / unique_name

    try:
        await run_sync(_sync_download, url, str(local_path))
        content_type = (
            attachment.content_type
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        return {
            "filename": name,
            "local_path": str(local_path),
            "content_type": content_type,
            "kind": classify(content_type),
        }
    except Exception:
        logger.exception("Failed to download attachment %s", name)
        return None


def build_media_prompt(user_text: str, saved_files: list[dict]) -> str:
    if not saved_files:
        return user_text

    descriptions = [
        f"[Attached {f['kind']}: {f['filename']} ({f['content_type']}), saved at {f['local_path']}]"
        for f in saved_files
    ]
    block = "\n".join(descriptions)
    return f"{block}\n\n{user_text}" if user_text else block
