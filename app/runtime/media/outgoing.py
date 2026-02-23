"""Outgoing media pipeline -- collect, resize, send, and error tracking."""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
import shutil
import uuid
from pathlib import Path

from botbuilder.schema import Attachment

from ..config.settings import cfg
from .classify import EXTENSION_TO_MIME

logger = logging.getLogger(__name__)

MAX_OUTGOING_FILE_BYTES = 190 * 1024

_RESIZABLE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def _try_resize_image(entry: Path, max_bytes: int) -> bool:
    if entry.suffix.lower() not in _RESIZABLE_EXTENSIONS:
        return False

    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed -- cannot auto-resize images")
        return False

    try:
        img = Image.open(entry)
        output_format = "JPEG"
        output_ext = ".jpg"
        if entry.suffix.lower() == ".png" and img.mode == "RGBA":
            output_format = "PNG"
            output_ext = ".png"
        elif entry.suffix.lower() == ".webp":
            output_format = "WEBP"
            output_ext = ".webp"

        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        original_size = entry.stat().st_size
        width, height = img.size

        for attempt in range(6):
            scale = 0.75 ** (attempt + 1)
            new_w = max(int(width * scale), 100)
            new_h = max(int(height * scale), 100)
            resized = img.resize((new_w, new_h), Image.LANCZOS)

            tmp_path = entry.with_suffix(f".tmp{output_ext}")
            save_kwargs: dict = {"format": output_format}
            if output_format in ("JPEG", "WEBP"):
                save_kwargs["quality"] = 85
            resized.save(tmp_path, **save_kwargs)

            if tmp_path.stat().st_size <= max_bytes:
                entry.unlink()
                final_path = entry.with_suffix(output_ext)
                tmp_path.rename(final_path)
                logger.info(
                    "Auto-resized %s: %s -> %s (%dx%d -> %dx%d)",
                    entry.name,
                    f"{original_size:,}B",
                    f"{final_path.stat().st_size:,}B",
                    width, height, new_w, new_h,
                )
                return True
            tmp_path.unlink()

        return False
    except Exception as exc:
        logger.warning("Auto-resize failed for %s: %s", entry.name, exc)
        for tmp in entry.parent.glob(f"{entry.stem}.tmp*"):
            tmp.unlink(missing_ok=True)
        return False


def _move_to_error(entry: Path, reason: str) -> None:
    error_dir = cfg.media_outgoing_error_dir
    error_dir.mkdir(parents=True, exist_ok=True)
    dest = error_dir / entry.name
    if dest.exists():
        dest = error_dir / f"{entry.stem}_{uuid.uuid4().hex[:6]}{entry.suffix}"
    shutil.move(str(entry), str(dest))
    dest.with_suffix(dest.suffix + ".error.txt").write_text(reason)
    logger.warning("Moved %s to error/: %s", entry.name, reason)


def collect_pending_outgoing() -> list[Attachment]:
    pending_dir = cfg.media_outgoing_pending_dir
    sent_dir = cfg.media_outgoing_sent_dir

    if not pending_dir.is_dir():
        return []

    attachments: list[Attachment] = []

    for entry in sorted(pending_dir.iterdir()):
        if not entry.is_file():
            continue

        try:
            file_size = entry.stat().st_size
        except OSError:
            _move_to_error(entry, "Could not stat file")
            continue

        if file_size > MAX_OUTGOING_FILE_BYTES:
            if _try_resize_image(entry, MAX_OUTGOING_FILE_BYTES):
                resized = next(
                    (c for c in entry.parent.glob(f"{entry.stem}.*")
                     if c.is_file() and c.suffix.lower() != ".tmp"),
                    None,
                )
                if resized:
                    entry = resized
                    file_size = entry.stat().st_size
                else:
                    _move_to_error(entry, _too_large_msg(file_size, "Auto-resize produced no output."))
                    continue
            else:
                _move_to_error(entry, _too_large_msg(file_size))
                continue

        content_type = (
            EXTENSION_TO_MIME.get(entry.suffix.lower())
            or mimetypes.guess_type(entry.name)[0]
            or "application/octet-stream"
        )

        try:
            data = base64.b64encode(entry.read_bytes()).decode("ascii")
            attachments.append(
                Attachment(
                    name=entry.name,
                    content_type=content_type,
                    content_url=f"data:{content_type};base64,{data}",
                )
            )
            dest = sent_dir / entry.name
            if dest.exists():
                dest = sent_dir / f"{entry.stem}_{uuid.uuid4().hex[:6]}{entry.suffix}"
            shutil.move(str(entry), str(dest))
            logger.info("Sent outgoing file %s -> %s", entry.name, dest)
        except Exception as exc:
            logger.exception("Failed to process pending file %s", entry.name)
            _move_to_error(entry, f"Processing error: {exc}")

    return attachments


def _too_large_msg(file_size: int, extra: str = "") -> str:
    msg = (
        f"File too large: {file_size:,} bytes "
        f"(limit is {MAX_OUTGOING_FILE_BYTES:,} bytes / ~190 KB)."
    )
    if extra:
        msg += f" {extra}"
    return msg


def move_attachments_to_error(attachments: list[Attachment], reason: str) -> None:
    error_dir = cfg.media_outgoing_error_dir
    error_dir.mkdir(parents=True, exist_ok=True)
    sent_dir = cfg.media_outgoing_sent_dir

    for att in attachments:
        name = att.name or "unknown"
        src = sent_dir / name
        if not src.is_file():
            candidates = list(sent_dir.glob(f"{Path(name).stem}_*{Path(name).suffix}"))
            src = candidates[-1] if candidates else None
        if src and src.is_file():
            dest = error_dir / src.name
            if dest.exists():
                dest = error_dir / f"{src.stem}_{uuid.uuid4().hex[:6]}{src.suffix}"
            shutil.move(str(src), str(dest))
            dest.with_suffix(dest.suffix + ".error.txt").write_text(reason)
            logger.warning("Transmission failed, moved %s to error/: %s", src.name, reason)


def read_error_details() -> list[dict]:
    error_dir = cfg.media_outgoing_error_dir
    if not error_dir.is_dir():
        return []

    details: list[dict] = []
    for err_file in sorted(error_dir.glob("*.error.txt")):
        try:
            reason = err_file.read_text().strip()
            original_name = err_file.name.replace(".error.txt", "")
            details.append({"filename": original_name, "reason": reason})
        except OSError:
            continue
    return details


# -- inline response attachment extraction ----------------------------------

_FILE_PATH_RE = re.compile(
    r"(?:^|\s)(/[\w./-]+\.(?:" + "|".join(ext.lstrip(".") for ext in EXTENSION_TO_MIME) + r"))\b",
    re.IGNORECASE,
)


def extract_outgoing_attachments(response: str) -> list[Attachment]:
    """Scan LLM response text for file paths and return base64-encoded attachments."""
    matches = _FILE_PATH_RE.findall(response)
    attachments: list[Attachment] = []
    seen: set[str] = set()

    for file_path in matches:
        if file_path in seen:
            continue
        seen.add(file_path)

        p = Path(file_path)
        if not p.is_file():
            continue

        content_type = EXTENSION_TO_MIME.get(p.suffix.lower())
        if not content_type:
            continue

        try:
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            attachments.append(
                Attachment(
                    name=p.name,
                    content_type=content_type,
                    content_url=f"data:{content_type};base64,{data}",
                )
            )
        except Exception:
            logger.exception("Failed to read media file %s", file_path)

    return attachments
