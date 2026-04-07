"""Tests for outgoing media pipeline."""

from __future__ import annotations

import base64
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botbuilder.schema import Attachment

from app.runtime.media.outgoing import (
    MAX_OUTGOING_FILE_BYTES,
    _move_to_error,
    collect_pending_outgoing,
    move_attachments_to_error,
    read_error_details,
)


class TestMoveToError:
    def test_moves_file(self, data_dir: Path):
        from app.runtime.config.settings import cfg

        pending = cfg.media_outgoing_pending_dir
        pending.mkdir(parents=True, exist_ok=True)
        f = pending / "test.txt"
        f.write_text("content")
        _move_to_error(f, "test reason")
        error_dir = cfg.media_outgoing_error_dir
        assert (error_dir / "test.txt").exists()
        assert (error_dir / "test.txt.error.txt").exists()
        assert "test reason" in (error_dir / "test.txt.error.txt").read_text()

    def test_duplicate_name(self, data_dir: Path):
        from app.runtime.config.settings import cfg

        error_dir = cfg.media_outgoing_error_dir
        error_dir.mkdir(parents=True, exist_ok=True)
        (error_dir / "dup.txt").write_text("existing")

        pending = cfg.media_outgoing_pending_dir
        pending.mkdir(parents=True, exist_ok=True)
        f = pending / "dup.txt"
        f.write_text("new")
        _move_to_error(f, "dup reason")
        assert len(list(error_dir.glob("dup*.txt"))) >= 2


class TestCollectPendingOutgoing:
    def test_empty_dir(self, data_dir: Path):
        result = collect_pending_outgoing()
        assert result == []

    def test_small_file(self, data_dir: Path):
        from app.runtime.config.settings import cfg

        pending = cfg.media_outgoing_pending_dir
        pending.mkdir(parents=True, exist_ok=True)
        sent = cfg.media_outgoing_sent_dir
        sent.mkdir(parents=True, exist_ok=True)
        f = pending / "small.png"
        f.write_bytes(b"\x89PNG" + b"\x00" * 100)
        result = collect_pending_outgoing()
        assert len(result) == 1
        assert result[0].name == "small.png"
        assert not f.exists()
        assert (sent / "small.png").exists()

    def test_large_non_resizable_file(self, data_dir: Path):
        from app.runtime.config.settings import cfg

        pending = cfg.media_outgoing_pending_dir
        pending.mkdir(parents=True, exist_ok=True)
        cfg.media_outgoing_error_dir.mkdir(parents=True, exist_ok=True)
        f = pending / "large.pdf"
        f.write_bytes(b"\x00" * (MAX_OUTGOING_FILE_BYTES + 100))
        result = collect_pending_outgoing()
        assert result == []
        assert not f.exists()

    def test_stat_error(self, data_dir: Path):
        from app.runtime.config.settings import cfg

        pending = cfg.media_outgoing_pending_dir
        pending.mkdir(parents=True, exist_ok=True)
        cfg.media_outgoing_error_dir.mkdir(parents=True, exist_ok=True)
        f = pending / "broken"
        f.mkdir()
        result = collect_pending_outgoing()
        assert result == []


class TestMoveAttachmentsToError:
    def test_moves_sent_to_error(self, data_dir: Path):
        from app.runtime.config.settings import cfg

        sent = cfg.media_outgoing_sent_dir
        sent.mkdir(parents=True, exist_ok=True)
        error = cfg.media_outgoing_error_dir
        error.mkdir(parents=True, exist_ok=True)
        (sent / "file.png").write_bytes(b"data")
        att = Attachment(name="file.png", content_type="image/png")
        move_attachments_to_error([att], "send failed")
        assert (error / "file.png").exists()
        assert (error / "file.png.error.txt").exists()

    def test_missing_sent_file_ignored(self, data_dir: Path):
        from app.runtime.config.settings import cfg

        cfg.media_outgoing_sent_dir.mkdir(parents=True, exist_ok=True)
        cfg.media_outgoing_error_dir.mkdir(parents=True, exist_ok=True)
        att = Attachment(name="gone.png", content_type="image/png")
        move_attachments_to_error([att], "reason")


class TestReadErrorDetails:
    def test_empty(self, data_dir: Path):
        assert read_error_details() == []

    def test_with_errors(self, data_dir: Path):
        from app.runtime.config.settings import cfg

        error_dir = cfg.media_outgoing_error_dir
        error_dir.mkdir(parents=True, exist_ok=True)
        (error_dir / "file1.png.error.txt").write_text("Too large")
        (error_dir / "file2.jpg.error.txt").write_text("Send failed")
        details = read_error_details()
        assert len(details) == 2
        assert details[0]["filename"] == "file1.png"
        assert details[0]["reason"] == "Too large"
