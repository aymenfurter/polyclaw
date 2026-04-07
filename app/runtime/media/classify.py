"""Media type classification and MIME-type registry."""

from __future__ import annotations

EXTENSION_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}

_KNOWN_MIMES = frozenset(EXTENSION_TO_MIME.values())


def classify(content_type: str) -> str:
    """Return ``'image'``, ``'audio'``, ``'video'``, or ``'file'``."""
    mime = content_type.lower().split(";")[0].strip()
    prefix = mime.split("/")[0]
    return prefix if prefix in ("image", "audio", "video") and mime in _KNOWN_MIMES else "file"
