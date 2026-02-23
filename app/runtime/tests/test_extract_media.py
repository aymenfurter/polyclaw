"""Tests for media extraction (extract_outgoing_attachments, download_attachment)."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.runtime.media.incoming import build_media_prompt
from app.runtime.media.outgoing import extract_outgoing_attachments


class TestExtractOutgoingAttachments:
    def test_no_paths(self):
        result = extract_outgoing_attachments("Hello, no file paths here.")
        assert result == []

    def test_with_valid_file(self, tmp_path: Path):
        img = tmp_path / "output.png"
        img.write_bytes(b"\x89PNG" + b"\x00" * 10)
        response = f"Here is your image: {img}"
        result = extract_outgoing_attachments(response)
        assert len(result) == 1
        assert result[0].name == "output.png"
        assert result[0].content_type == "image/png"
        assert result[0].content_url.startswith("data:image/png;base64,")

    def test_nonexistent_file(self):
        response = "Here: /tmp/nonexistent_file_12345.png"
        result = extract_outgoing_attachments(response)
        assert result == []

    def test_dedup_paths(self, tmp_path: Path):
        img = tmp_path / "dup.jpg"
        img.write_bytes(b"\xff\xd8" + b"\x00" * 10)
        response = f"{img} and again {img}"
        result = extract_outgoing_attachments(response)
        assert len(result) == 1


class TestBuildMediaPromptExtended:
    def test_with_files(self):
        files = [
            {"kind": "image", "filename": "photo.jpg", "content_type": "image/jpeg", "local_path": "/tmp/photo.jpg"},
        ]
        result = build_media_prompt("Please analyze", files)
        assert "Attached image" in result
        assert "photo.jpg" in result
        assert "Please analyze" in result

    def test_files_only_no_text(self):
        files = [
            {"kind": "document", "filename": "doc.pdf", "content_type": "application/pdf", "local_path": "/tmp/doc.pdf"},
        ]
        result = build_media_prompt("", files)
        assert "Attached document" in result
        assert "doc.pdf" in result
