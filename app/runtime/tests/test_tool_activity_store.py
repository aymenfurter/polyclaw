"""Tests for the tool activity store."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.runtime.state.tool_activity_store import ToolActivityStore


class TestToolActivityStore:
    """Tests for ToolActivityStore."""

    def test_record_start(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        entry = store.record_start(
            session_id="sess-1",
            tool="bash",
            call_id="c1",
            arguments="echo hello",
        )
        assert entry.id == "ta-1"
        assert entry.tool == "bash"
        assert entry.status == "started"
        assert entry.session_id == "sess-1"
        assert entry.category == "sdk"

    def test_record_complete(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        store.record_start(session_id="s1", tool="run", call_id="c1", arguments="ls")
        entry = store.record_complete(call_id="c1", result="file1.txt\nfile2.txt")
        assert entry is not None
        assert entry.status == "completed"
        assert entry.result == "file1.txt\nfile2.txt"
        assert entry.duration_ms is not None
        assert entry.duration_ms >= 0

    def test_query_empty(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        result = store.query()
        assert result["total"] == 0
        assert result["entries"] == []

    def test_query_with_filters(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        store.record_start(session_id="s1", tool="bash", call_id="c1")
        store.record_start(session_id="s1", tool="mcp__search", call_id="c2")
        store.record_start(session_id="s2", tool="bash", call_id="c3")

        result = store.query(session_id="s1")
        assert result["total"] == 2

        result = store.query(tool="bash")
        assert result["total"] == 2

        result = store.query(category="mcp")
        assert result["total"] == 1

    def test_flagging(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        entry = store.record_start(session_id="s1", tool="bash", call_id="c1")
        assert not entry.flagged

        store.flag_entry(entry.id, "looks suspicious")
        result = store.query(flagged_only=True)
        assert result["total"] == 1
        assert result["entries"][0]["flag_reason"] == "looks suspicious"

    def test_flag_completed_entry_after_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "activity.jsonl"
        store1 = ToolActivityStore(path)
        store1.record_start(session_id="s1", tool="bash", call_id="c1")
        store1.record_complete(call_id="c1", result="done")

        # Reload from disk (simulates server restart)
        store2 = ToolActivityStore(path)
        result = store2.query()
        assert result["total"] == 1
        entry_id = result["entries"][0]["id"]
        assert not result["entries"][0]["flagged"]

        # Flag the completed entry
        assert store2.flag_entry(entry_id, "suspicious")
        result = store2.query(flagged_only=True)
        assert result["total"] == 1
        assert result["entries"][0]["flagged"]

        # Verify persistence across another reload
        store3 = ToolActivityStore(path)
        result = store3.query(flagged_only=True)
        assert result["total"] == 1

    def test_suspicious_detection(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        entry = store.record_start(
            session_id="s1", tool="bash", call_id="c1",
            arguments="rm -rf /",
        )
        assert entry.flagged
        assert "rm -rf" in entry.flag_reason

    def test_summary(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        store.record_start(session_id="s1", tool="bash", call_id="c1")
        store.record_start(session_id="s1", tool="edit", call_id="c2")
        store.record_start(session_id="s2", tool="bash", call_id="c3")

        summary = store.get_summary()
        assert summary["total"] == 3
        assert summary["sessions_with_activity"] == 2
        assert summary["by_tool"]["bash"] == 2
        assert summary["by_tool"]["edit"] == 1

    def test_get_entry(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        entry = store.record_start(session_id="s1", tool="grep", call_id="c1")
        fetched = store.get_entry(entry.id)
        assert fetched is not None
        assert fetched["tool"] == "grep"

    def test_persistence(self, tmp_path: Path) -> None:
        path = tmp_path / "activity.jsonl"
        store1 = ToolActivityStore(path)
        store1.record_start(session_id="s1", tool="run", call_id="c1")
        store1.record_complete(call_id="c1", result="done")

        # Load from disk
        store2 = ToolActivityStore(path)
        result = store2.query()
        assert result["total"] >= 1

    def test_infer_category(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        assert store._infer_category("bash") == "sdk"
        assert store._infer_category("run") == "sdk"
        assert store._infer_category("my__tool") == "mcp"
        assert store._infer_category("server.search") == "mcp"
        assert store._infer_category("web_search") == "custom"

    def test_model_tracking(self, tmp_path: Path) -> None:
        store = ToolActivityStore(tmp_path / "activity.jsonl")
        store.record_start(session_id="s1", tool="bash", call_id="c1", model="gpt-4o")
        store.record_start(session_id="s1", tool="edit", call_id="c2", model="gpt-4o")
        store.record_start(session_id="s2", tool="bash", call_id="c3", model="gpt-4o-mini")

        summary = store.get_summary()
        assert summary["by_model"]["gpt-4o"] == 2
        assert summary["by_model"]["gpt-4o-mini"] == 1

        result = store.query(model="gpt-4o-mini")
        assert result["total"] == 1
        assert result["entries"][0]["model"] == "gpt-4o-mini"

        sessions = store.get_session_breakdown()
        s1 = next(s for s in sessions if s["session_id"] == "s1")
        assert "gpt-4o" in s1["models"]
        s2 = next(s for s in sessions if s["session_id"] == "s2")
        assert "gpt-4o-mini" in s2["models"]
