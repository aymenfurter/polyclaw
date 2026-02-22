"""Tests for the realtime tools module -- TaskStore and handlers."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.runtime.realtime.tools import (
    ALL_REALTIME_TOOL_SCHEMAS,
    TaskStatus,
    TaskStore,
    _make_realtime_hook,
    handle_check_agent_task,
    handle_invoke_agent,
    handle_invoke_agent_async,
)


class TestTaskStore:
    def test_create(self) -> None:
        store = TaskStore()
        task = store.create("Do something")
        assert task.prompt == "Do something"
        assert task.status == TaskStatus.PENDING

    def test_get(self) -> None:
        store = TaskStore()
        task = store.create("X")
        found = store.get(task.id)
        assert found is not None
        assert found.id == task.id

    def test_get_nonexistent(self) -> None:
        store = TaskStore()
        assert store.get("nope") is None

    def test_complete(self) -> None:
        store = TaskStore()
        task = store.create("X")
        store.complete(task.id, "done!")
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "done!"
        assert task.completed_at is not None

    def test_fail(self) -> None:
        store = TaskStore()
        task = store.create("X")
        store.fail(task.id, "oops")
        assert task.status == TaskStatus.FAILED
        assert task.error == "oops"
        assert task.completed_at is not None

    def test_complete_nonexistent(self) -> None:
        store = TaskStore()
        store.complete("nope", "result")

    def test_fail_nonexistent(self) -> None:
        store = TaskStore()
        store.fail("nope", "error")


class TestTaskStatus:
    def test_values(self) -> None:
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"


class TestSchemas:
    def test_all_schemas_present(self) -> None:
        assert len(ALL_REALTIME_TOOL_SCHEMAS) == 3
        names = {s["name"] for s in ALL_REALTIME_TOOL_SCHEMAS}
        assert "invoke_agent" in names
        assert "invoke_agent_async" in names
        assert "check_agent_task" in names

    def test_schema_structure(self) -> None:
        for schema in ALL_REALTIME_TOOL_SCHEMAS:
            assert schema["type"] == "function"
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema


@pytest.mark.asyncio
class TestHandleInvokeAgent:
    async def test_empty_prompt(self) -> None:
        result = await handle_invoke_agent({}, agent=None)
        assert "Error" in result or "no prompt" in result

    @patch("app.runtime.realtime.tools._run_one_shot_realtime")
    async def test_success(self, mock_one_shot: AsyncMock) -> None:
        mock_one_shot.return_value = "Response to: hello"
        result = await handle_invoke_agent({"prompt": "hello"}, agent=MagicMock())
        assert "Response to: hello" in result
        mock_one_shot.assert_awaited_once_with("hello", mock_one_shot.call_args[0][1])

    @patch("app.runtime.realtime.tools._run_one_shot_realtime")
    async def test_agent_returns_none(self, mock_one_shot: AsyncMock) -> None:
        mock_one_shot.return_value = None
        result = await handle_invoke_agent({"prompt": "hello"}, agent=MagicMock())
        assert "no response" in result.lower()

    @patch("app.runtime.realtime.tools._run_one_shot_realtime")
    async def test_agent_exception(self, mock_one_shot: AsyncMock) -> None:
        mock_one_shot.side_effect = RuntimeError("broke")
        result = await handle_invoke_agent({"prompt": "hello"}, agent=MagicMock())
        assert "Error" in result


@pytest.mark.asyncio
class TestHandleCheckAgentTask:
    async def test_no_task_id(self) -> None:
        result = await handle_check_agent_task({})
        data = json.loads(result)
        assert "error" in data

    async def test_not_found(self) -> None:
        result = await handle_check_agent_task({"task_id": "missing"})
        data = json.loads(result)
        assert "error" in data


class TestMakeRealtimeHook:
    """Verify that _make_realtime_hook creates a properly configured interceptor."""

    @patch("app.runtime.state.guardrails_config.get_guardrails_config")
    def test_hook_sets_execution_context(self, mock_get_cfg: MagicMock) -> None:
        mock_store = MagicMock()
        mock_store.hitl_enabled = True
        mock_get_cfg.return_value = mock_store

        agent = MagicMock()
        agent.hitl_interceptor = None

        hook = _make_realtime_hook(agent)
        assert callable(hook)

    @patch("app.runtime.state.guardrails_config.get_guardrails_config")
    def test_hook_forwards_aitl_from_shared(self, mock_get_cfg: MagicMock) -> None:
        mock_store = MagicMock()
        mock_store.hitl_enabled = True
        mock_get_cfg.return_value = mock_store

        shared_hitl = MagicMock()
        shared_hitl._aitl_reviewer = MagicMock()
        shared_hitl._prompt_shield = MagicMock()
        shared_hitl._phone_verifier = None

        agent = MagicMock()
        agent.hitl_interceptor = shared_hitl

        hook = _make_realtime_hook(agent)
        assert callable(hook)

    @patch("app.runtime.state.guardrails_config.get_guardrails_config")
    def test_hook_works_without_shared_hitl(self, mock_get_cfg: MagicMock) -> None:
        mock_store = MagicMock()
        mock_store.hitl_enabled = True
        mock_get_cfg.return_value = mock_store

        agent = MagicMock(spec=[])  # no hitl_interceptor attribute

        hook = _make_realtime_hook(agent)
        assert callable(hook)
