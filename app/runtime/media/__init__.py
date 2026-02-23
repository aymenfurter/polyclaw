"""Media handling -- type classification, download, and outgoing extraction."""

from .classify import EXTENSION_TO_MIME, classify
from .incoming import build_media_prompt, download_attachment
from .outgoing import (
    MAX_OUTGOING_FILE_BYTES,
    collect_pending_outgoing,
    extract_outgoing_attachments,
    move_attachments_to_error,
    read_error_details,
)

__all__ = [
    "EXTENSION_TO_MIME",
    "MAX_OUTGOING_FILE_BYTES",
    "build_media_prompt",
    "classify",
    "collect_pending_outgoing",
    "download_attachment",
    "extract_outgoing_attachments",
    "move_attachments_to_error",
    "read_error_details",
]
