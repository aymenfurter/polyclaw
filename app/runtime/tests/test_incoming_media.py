"""Tests for incoming media and classify helpers."""

from __future__ import annotations

import re
from pathlib import Path

from app.runtime.media.incoming import build_media_prompt
from app.runtime.media.outgoing import _FILE_PATH_RE


class TestBuildMediaPrompt:
    def test_no_files(self) -> None:
        assert build_media_prompt("hello", []) == "hello"

    def test_with_files(self) -> None:
        files = [
            {
                "filename": "photo.jpg",
                "local_path": "/tmp/photo.jpg",
                "content_type": "image/jpeg",
                "kind": "image",
            }
        ]
        result = build_media_prompt("caption", files)
        assert "photo.jpg" in result
        assert "caption" in result

    def test_only_files_no_text(self) -> None:
        files = [
            {
                "filename": "doc.pdf",
                "local_path": "/tmp/doc.pdf",
                "content_type": "application/pdf",
                "kind": "file",
            }
        ]
        result = build_media_prompt("", files)
        assert "doc.pdf" in result

    def test_multiple_files(self) -> None:
        files = [
            {"filename": "a.png", "local_path": "/a.png", "content_type": "image/png", "kind": "image"},
            {"filename": "b.mp3", "local_path": "/b.mp3", "content_type": "audio/mpeg", "kind": "audio"},
        ]
        result = build_media_prompt("look", files)
        assert "a.png" in result
        assert "b.mp3" in result
        assert "look" in result


class TestFilePathRegex:
    def test_matches_image_path(self) -> None:
        text = "saved at /data/media/photo.jpg done"
        matches = _FILE_PATH_RE.findall(text)
        assert any("photo.jpg" in m for m in matches)

    def test_matches_audio_path(self) -> None:
        text = "file: /tmp/song.mp3"
        matches = _FILE_PATH_RE.findall(text)
        assert any("song.mp3" in m for m in matches)

    def test_no_match_for_non_media(self) -> None:
        text = "file at /tmp/data.txt"
        matches = _FILE_PATH_RE.findall(text)
        assert len(matches) == 0
