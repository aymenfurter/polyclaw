"""Sandbox tool interceptor -- intercepts shell tool calls for sandbox execution."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from .executor import SandboxExecutor
from .helpers import _build_replay_command, _extract_command, _is_shell_tool, _parse_tool_args

logger = logging.getLogger(__name__)

_SESSION_IDLE_TIMEOUT = 60


class SandboxToolInterceptor:
    def __init__(self, executor: SandboxExecutor) -> None:
        self._executor = executor
        self._session_id: str | None = None
        self._session_ready: bool = False
        self._provisioning: bool = False
        self._last_activity: float = 0
        self._idle_task: asyncio.Task | None = None
        self._pending_result: dict[str, Any] | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def _ensure_session(self) -> str:
        self._last_activity = time.time()
        if self._session_id and self._session_ready:
            return self._session_id

        self._session_id = str(uuid.uuid4())
        self._session_ready = False
        self._provisioning = True

        try:
            result = await self._executor.provision_session(self._session_id)
            if not result["success"]:
                self._session_id = None
                raise RuntimeError(f"Sandbox session provision failed: {result.get('error')}")
            self._session_ready = True
        finally:
            self._provisioning = False

        self._start_idle_timer()
        return self._session_id

    async def _teardown_session(self) -> None:
        if not self._session_id:
            return
        sid = self._session_id
        self._session_id = None
        self._session_ready = False
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None
        try:
            await self._executor.destroy_session(sid)
        except Exception as exc:
            logger.warning("Session teardown error: %s", exc)

    def _start_idle_timer(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.ensure_future(self._idle_reaper())

    async def _idle_reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                if not self._session_id:
                    return
                if time.time() - self._last_activity >= _SESSION_IDLE_TIMEOUT:
                    await self._teardown_session()
                    return
        except asyncio.CancelledError:
            pass

    def touch(self) -> None:
        self._last_activity = time.time()

    async def on_pre_tool_use(self, input_data: dict, ctx: dict) -> dict | None:
        tool_name = input_data.get("toolName", "")
        if not self._executor.enabled:
            return {"permissionDecision": "allow"}
        if not _is_shell_tool(tool_name):
            return {"permissionDecision": "allow"}

        tool_args = _parse_tool_args(input_data.get("toolArgs"))
        command = _extract_command(tool_args)
        if not command:
            return {"permissionDecision": "allow"}

        try:
            session_id = await self._ensure_session()
            result = await self._executor.run_in_session(session_id, command, timeout=120)
            self._last_activity = time.time()
        except Exception as exc:
            logger.error("Sandbox interceptor failed: %s", exc, exc_info=True)
            result = {"success": False, "stdout": "", "stderr": str(exc)}

        self._pending_result = result
        replay = _build_replay_command(
            result.get("stdout", ""), result.get("stderr", ""), result.get("success", False)
        )
        noop_args = dict(tool_args)
        noop_args["command"] = replay
        if "input" in noop_args:
            noop_args["input"] = replay
        return {"permissionDecision": "allow", "modifiedArgs": noop_args}

    async def on_post_tool_use(self, input_data: dict, ctx: dict) -> dict | None:
        if self._pending_result is None:
            return None

        result = self._pending_result
        self._pending_result = None

        parts: list[str] = []
        if result.get("stdout"):
            parts.append(result["stdout"])
        if result.get("stderr"):
            parts.append(f"STDERR:\n{result['stderr']}")
        output = "\n".join(parts) if parts else "(no output)"
        if not result.get("success"):
            output = f"Command failed in sandbox.\n{output}"
        return {"modifiedResult": output}
