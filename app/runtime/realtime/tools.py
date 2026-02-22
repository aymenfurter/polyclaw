"""Agent bridge tools for the Realtime voice model."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..config.settings import cfg

logger = logging.getLogger(__name__)

_REALTIME_MODEL = "gpt-4.1"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentTask:
    id: str
    prompt: str
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None


class TaskStore:
    """In-memory store for async agent tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, AgentTask] = {}

    def create(self, prompt: str) -> AgentTask:
        task = AgentTask(id=str(uuid.uuid4())[:8], prompt=prompt)
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> AgentTask | None:
        return self._tasks.get(task_id)

    def complete(self, task_id: str, result: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.COMPLETED
            task.result = result
            task.completed_at = datetime.now(UTC).isoformat()

    def fail(self, task_id: str, error: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.status = TaskStatus.FAILED
            task.error = error
            task.completed_at = datetime.now(UTC).isoformat()


_task_store: TaskStore | None = None


def get_task_store() -> TaskStore:
    global _task_store
    if _task_store is None:
        _task_store = TaskStore()
    return _task_store


def _reset_task_store() -> None:
    global _task_store
    _task_store = None


INVOKE_AGENT_SCHEMA = {
    "type": "function",
    "name": "invoke_agent",
    "description": (
        "Invoke the coding agent SYNCHRONOUSLY for quick tasks. "
        "Use this for simple requests that should return in a few seconds: "
        "checking the time, quick lookups, simple math, reading a file, "
        "checking status, or any task with a short answer. "
        "The agent has full access to tools, memory, skills, browser, "
        "and the internet. You MUST wait for the result before responding."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The task or question to send to the agent.",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}

INVOKE_AGENT_ASYNC_SCHEMA = {
    "type": "function",
    "name": "invoke_agent_async",
    "description": (
        "Invoke the coding agent ASYNCHRONOUSLY for longer tasks. "
        "Use this for complex requests that may take a while: research, "
        "multi-step operations, web browsing, code generation, analysis, "
        "creating files, or anything that might take more than 10 seconds. "
        "Returns a task ID immediately. Use check_agent_task to poll for results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The task or question to send to the agent.",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}

CHECK_AGENT_TASK_SCHEMA = {
    "type": "function",
    "name": "check_agent_task",
    "description": (
        "Check the status of a previously submitted async agent task. "
        "Returns the current status (pending, running, completed, failed) "
        "and the result if completed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID returned by invoke_agent_async.",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
}

ALL_REALTIME_TOOL_SCHEMAS = [
    INVOKE_AGENT_SCHEMA,
    INVOKE_AGENT_ASYNC_SCHEMA,
    CHECK_AGENT_TASK_SCHEMA,
]


async def handle_invoke_agent(args: dict[str, Any], agent: Any) -> str:
    prompt = args.get("prompt", "")
    if not prompt:
        return "Error: no prompt provided."

    logger.info("Realtime sync invoke: %s", prompt[:100])
    try:
        result = await asyncio.wait_for(
            _run_one_shot_realtime(prompt, agent),
            timeout=60.0,
        )
        return result or "(no response from agent)"
    except TimeoutError:
        return "Agent task timed out after 60 seconds."
    except Exception as exc:
        logger.error("Sync agent invoke failed: %s", exc, exc_info=True)
        return f"Error: {exc}"


async def handle_invoke_agent_async(args: dict[str, Any], agent: Any) -> str:
    prompt = args.get("prompt", "")
    if not prompt:
        return '{"error": "no prompt provided"}'

    store = get_task_store()
    task = store.create(prompt)
    task.status = TaskStatus.RUNNING

    logger.info("Realtime async invoke [%s]: %s", task.id, prompt[:100])

    async def _run() -> None:
        try:
            result = await _run_one_shot_realtime(prompt, agent)
            store.complete(task.id, result or "(no response from agent)")
            logger.info("Async task %s completed", task.id)
        except Exception as exc:
            store.fail(task.id, str(exc))
            logger.error("Async task %s failed: %s", task.id, exc)

    asyncio.create_task(_run())
    return json.dumps({
        "task_id": task.id,
        "status": "running",
        "message": "Task submitted. Use check_agent_task to poll for results.",
    })


async def handle_check_agent_task(args: dict[str, Any]) -> str:
    task_id = args.get("task_id", "")
    if not task_id:
        return '{"error": "no task_id provided"}'

    store = get_task_store()
    task = store.get(task_id)
    if not task:
        return json.dumps({"error": f"task {task_id} not found"})

    response: dict[str, Any] = {"task_id": task.id, "status": task.status.value}
    if task.status == TaskStatus.COMPLETED:
        response["result"] = task.result
    elif task.status == TaskStatus.FAILED:
        response["error"] = task.error
    return json.dumps(response)


# ------------------------------------------------------------------
# One-shot session runner with realtime HITL hook
# ------------------------------------------------------------------


def _make_realtime_hook(
    agent: Any,
) -> Callable[[dict, Any], Awaitable[dict]]:
    """Build a guardrails-aware pre-tool-use hook for realtime sessions.

    Creates a fresh ``HitlInterceptor`` with ``execution_context`` set to
    ``"realtime"``.  AITL, Prompt Shields, and phone verifier are forwarded
    from the shared interceptor on the agent (if available).

    This mirrors the scheduler's ``_make_background_hook`` pattern so that
    guardrails policies are respected during voice-initiated tasks.
    """
    from ..agent.hitl import HitlInterceptor
    from ..state.guardrails_config import get_guardrails_config

    store = get_guardrails_config()
    interceptor = HitlInterceptor(store)
    interceptor.set_execution_context("realtime")
    interceptor.set_model(_REALTIME_MODEL)

    # Forward AITL / Prompt Shield / phone from the shared interceptor.
    shared_hitl = getattr(agent, "hitl_interceptor", None)
    if shared_hitl:
        if getattr(shared_hitl, "_aitl_reviewer", None):
            interceptor.set_aitl_reviewer(shared_hitl._aitl_reviewer)
        if getattr(shared_hitl, "_prompt_shield", None):
            interceptor.set_prompt_shield(shared_hitl._prompt_shield)
        if getattr(shared_hitl, "_phone_verifier", None):
            interceptor.set_phone_verifier(shared_hitl._phone_verifier)

    return interceptor.on_pre_tool_use


async def _run_one_shot_realtime(prompt: str, agent: Any) -> str | None:
    """Spawn an ephemeral Copilot session with realtime guardrails.

    Uses ``run_one_shot`` with the full tool set and a HITL hook that has
    ``execution_context="realtime"`` so guardrails policies can distinguish
    voice-initiated tool calls from interactive or background ones.
    """
    from ..agent import one_shot as one_shot_mod
    from ..agent.tools import get_all_tools

    hook = _make_realtime_hook(agent)

    return await one_shot_mod.run_one_shot(
        prompt,
        model=cfg.copilot_model,
        tools=get_all_tools(),
        on_pre_tool_use=hook,
    )
