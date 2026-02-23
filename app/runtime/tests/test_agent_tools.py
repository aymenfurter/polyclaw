"""Tests for agent tools module -- parameter models and tool handlers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _call_tool(tool, args: dict | None = None):
    """Helper to invoke a @define_tool-wrapped tool synchronously.

    The copilot SDK handler is async and wraps the return value in
    ``{"textResultForLlm": <json>, "resultType": "success"|"failure"}``.
    This helper runs the handler and returns the deserialised inner value.
    """
    import asyncio

    inv = {
        "session_id": "test",
        "tool_call_id": "tc",
        "tool_name": tool.name,
        "arguments": args or {},
    }
    raw = asyncio.run(tool.handler(inv))
    text = raw.get("textResultForLlm", "")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


class TestToolParameterModels:
    def test_schedule_task_params(self):
        from app.runtime.agent.tools import ScheduleTaskParams

        p = ScheduleTaskParams(
            description="Daily report",
            prompt="Generate report",
            cron="0 9 * * *",
        )
        assert p.description == "Daily report"
        assert p.prompt == "Generate report"
        assert p.cron == "0 9 * * *"
        assert p.run_at is None

    def test_schedule_task_one_shot(self):
        from app.runtime.agent.tools import ScheduleTaskParams

        p = ScheduleTaskParams(
            description="One-time",
            prompt="Do thing",
            run_at="2026-02-07T14:00:00",
        )
        assert p.run_at == "2026-02-07T14:00:00"
        assert p.cron is None

    def test_cancel_task_params(self):
        from app.runtime.agent.tools import CancelTaskParams

        p = CancelTaskParams(task_id="abc123")
        assert p.task_id == "abc123"

    def test_make_call_params_defaults(self):
        from app.runtime.agent.tools import MakeCallParams

        p = MakeCallParams()
        assert p.prompt is None
        assert p.opening_message is None

    def test_make_call_params_with_values(self):
        from app.runtime.agent.tools import MakeCallParams

        p = MakeCallParams(prompt="Be friendly", opening_message="Hi there!")
        assert p.prompt == "Be friendly"
        assert p.opening_message == "Hi there!"

    def test_search_memories_params(self):
        from app.runtime.agent.tools import SearchMemoriesParams

        p = SearchMemoriesParams(query="important meeting")
        assert p.query == "important meeting"
        assert p.top == 5

    def test_search_memories_params_custom_top(self):
        from app.runtime.agent.tools import SearchMemoriesParams

        p = SearchMemoriesParams(query="test", top=3)
        assert p.top == 3


class TestScheduleTaskTool:
    def test_schedule_task_success(self):
        from app.runtime.agent.tools import schedule_task

        result = _call_tool(schedule_task, {
            "description": "Test job",
            "prompt": "Do it",
            "cron": "0 9 * * *",
        })
        assert "id" in result
        assert result["status"] == "scheduled"

    def test_schedule_task_invalid_cron(self):
        from app.runtime.agent.tools import schedule_task

        result = _call_tool(schedule_task, {
            "description": "Bad",
            "prompt": "x",
            "cron": "* * * * *",
        })
        assert "error" in result


class TestCancelTaskTool:
    def test_cancel_existing(self):
        from app.runtime.agent.tools import cancel_task
        from app.runtime.scheduler import get_scheduler

        sched = get_scheduler()
        task = sched.add(description="to cancel", prompt="x", cron="0 9 * * *")
        result = _call_tool(cancel_task, {"task_id": task.id})
        assert "cancelled" in result

    def test_cancel_nonexistent(self):
        from app.runtime.agent.tools import cancel_task

        result = _call_tool(cancel_task, {"task_id": "nonexistent"})
        assert "not found" in result


class TestListScheduledTasks:
    def test_list_empty(self):
        from app.runtime.agent.tools import list_scheduled_tasks

        result = _call_tool(list_scheduled_tasks)
        assert result == []

    def test_list_with_tasks(self):
        from app.runtime.agent.tools import list_scheduled_tasks
        from app.runtime.scheduler import get_scheduler

        sched = get_scheduler()
        sched.add(description="task 1", prompt="p1", cron="0 9 * * *")
        sched.add(description="task 2", prompt="p2", cron="0 10 * * *")
        result = _call_tool(list_scheduled_tasks)
        assert len(result) == 2
        assert result[0]["description"] == "task 1"


class TestMakeVoiceCallTool:
    @patch("app.runtime.agent.tools.voice.cfg")
    def test_no_target_number(self, mock_cfg):
        from app.runtime.agent.tools import make_voice_call

        mock_cfg.voice_target_number = ""
        result = _call_tool(make_voice_call, {"prompt": "hi"})
        assert result["status"] == "error"

    @patch("app.runtime.agent.tools.voice.threading.Thread")
    @patch("app.runtime.agent.tools.voice.cfg")
    def test_with_target_number(self, mock_cfg, mock_thread):
        from app.runtime.agent.tools import make_voice_call

        mock_cfg.voice_target_number = "+1234567890"
        mock_cfg.admin_port = 8080
        mock_cfg.admin_secret = ""
        result = _call_tool(make_voice_call, {"prompt": "hi"})
        assert result["status"] == "ok"
        mock_thread.return_value.start.assert_called_once()


class TestSearchMemoriesTool:
    @patch("app.runtime.state.foundry_iq_config.get_foundry_iq_config")
    def test_foundry_iq_disabled(self, mock_config):
        from app.runtime.agent.tools import search_memories_tool

        mock_config.return_value.enabled = False
        result = _call_tool(search_memories_tool, {"query": "test"})
        assert result["status"] == "skipped"

    @patch("app.runtime.services.foundry_iq.search_memories")
    @patch("app.runtime.state.foundry_iq_config.get_foundry_iq_config")
    def test_search_ok(self, mock_config, mock_search):
        from app.runtime.agent.tools import search_memories_tool

        mock_config.return_value.enabled = True
        mock_config.return_value.is_configured = True
        mock_search.return_value = {
            "status": "ok",
            "results": [
                {"title": "meeting", "content": "notes", "source_type": "daily", "date": "2025-01-01"}
            ],
        }
        result = _call_tool(search_memories_tool, {"query": "meeting", "top": 3})
        assert result["status"] == "ok"
        assert result["count"] == 1

    @patch("app.runtime.services.foundry_iq.search_memories")
    @patch("app.runtime.state.foundry_iq_config.get_foundry_iq_config")
    def test_search_no_results(self, mock_config, mock_search):
        from app.runtime.agent.tools import search_memories_tool

        mock_config.return_value.enabled = True
        mock_config.return_value.is_configured = True
        mock_search.return_value = {"status": "ok", "results": []}
        result = _call_tool(search_memories_tool, {"query": "nothing"})
        assert result["count"] == 0

    @patch("app.runtime.services.foundry_iq.search_memories")
    @patch("app.runtime.state.foundry_iq_config.get_foundry_iq_config")
    def test_search_error(self, mock_config, mock_search):
        from app.runtime.agent.tools import search_memories_tool

        mock_config.return_value.enabled = True
        mock_config.return_value.is_configured = True
        mock_search.side_effect = RuntimeError("network error")
        result = _call_tool(search_memories_tool, {"query": "test"})
        assert result["status"] == "error"


class TestGetAllTools:
    def test_returns_tools_list(self):
        from app.runtime.agent.tools import ALL_TOOLS

        assert len(ALL_TOOLS) >= 4

    @patch("app.runtime.state.foundry_iq_config.get_foundry_iq_config")
    def test_includes_search_when_enabled(self, mock_config):
        from app.runtime.agent.tools import get_all_tools

        mock_config.return_value.enabled = True
        mock_config.return_value.is_configured = True
        tools = get_all_tools()
        assert len(tools) >= 5

    @patch("app.runtime.state.foundry_iq_config.get_foundry_iq_config")
    def test_excludes_search_when_disabled(self, mock_config):
        from app.runtime.agent.tools import get_all_tools

        mock_config.return_value.enabled = False
        tools = get_all_tools()
        from app.runtime.agent.tools import ALL_TOOLS
        assert len(tools) == len(ALL_TOOLS)
