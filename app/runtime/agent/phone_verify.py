"""Phone-call verification agent for HITL approval."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from ..config.settings import cfg

logger = logging.getLogger(__name__)

_PHONE_VERIFY_TIMEOUT = 300.0

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _load_template(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text()


class PhoneVerifier:

    def __init__(self, app: web.Application) -> None:
        self._app = app
        self._pending: dict[str, asyncio.Future[bool]] = {}

    @property
    def configured(self) -> bool:
        return bool(cfg.voice_target_number and self._app.get("_voice_handler"))

    @property
    def phone_number(self) -> str:
        """The target phone number, read live from global settings."""
        return cfg.voice_target_number

    async def request_verification(
        self,
        call_id: str,
        tool_name: str,
        tool_args: str,
    ) -> bool:
        if not self.configured:
            logger.warning(
                "[phone_verify] not configured (voice_target_number=%r, "
                "voice_handler=%s), auto-denying call_id=%s",
                cfg.voice_target_number,
                bool(self._app.get("_voice_handler")),
                call_id,
            )
            return False

        voice_handler = self._app.get("_voice_handler")
        if voice_handler is None:
            logger.warning("[phone_verify] voice handler gone, auto-denying")
            return False

        prompt = _load_template("phone_verify_prompt.md").format(
            tool_name=tool_name,
            tool_args=tool_args[:500],
        )
        opening = _load_template("phone_verify_opening.md").strip().format(
            tool_name=tool_name,
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[call_id] = future

        middleware = voice_handler._middleware
        cleanup = _register_verify_tools(middleware, call_id, self)

        logger.info(
            "[phone_verify] setting exclusive prompt: call_id=%s tool=%s "
            "tools=%d prompt_len=%d opening_len=%d",
            call_id, tool_name, len(VERIFY_TOOL_SCHEMAS),
            len(prompt), len(opening),
        )
        middleware.set_pending_prompt(
            prompt,
            opening_message=opening,
            tools=VERIFY_TOOL_SCHEMAS,
            exclusive=True,
        )

        target = cfg.voice_target_number
        try:
            caller = voice_handler._caller
            logger.info(
                "[phone_verify] initiating call: number=%s tool=%s call_id=%s",
                target, tool_name, call_id,
            )
            await caller.initiate_call(target)
            logger.info(
                "[phone_verify] call initiated successfully, waiting for "
                "decision: call_id=%s",
                call_id,
            )
        except Exception:
            logger.exception("[phone_verify] call initiation failed")
            self._pending.pop(call_id, None)
            cleanup()
            return False

        try:
            approved = await asyncio.wait_for(future, timeout=_PHONE_VERIFY_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "[phone_verify] timed out waiting for decision: call_id=%s", call_id,
            )
            approved = False
        finally:
            self._pending.pop(call_id, None)
            cleanup()
            logger.info(
                "[phone_verify] cleaned up verify tools: call_id=%s", call_id,
            )

        logger.info(
            "[phone_verify] decision: call_id=%s approved=%s", call_id, approved,
        )
        return approved

    def resolve(self, call_id: str, approved: bool) -> bool:
        future = self._pending.get(call_id)
        if future and not future.done():
            future.set_result(approved)
            logger.info(
                "[phone_verify.resolve] resolved: call_id=%s approved=%s",
                call_id, approved,
            )
            return True
        logger.warning("[phone_verify.resolve] no pending future: call_id=%s", call_id)
        return False


def _register_verify_tools(
    middleware: Any,
    call_id: str,
    verifier: PhoneVerifier,
) -> Callable[[], None]:
    original_execute = middleware._execute_tool

    async def patched_execute(item: dict[str, Any], server_ws: Any) -> None:
        name = item.get("name", "")
        tool_call_id = item.get("call_id", "")
        if name == "accept_operation":
            logger.info(
                "[phone_verify.tool] accept_operation: call_id=%s", call_id,
            )
            verifier.resolve(call_id, True)
            await server_ws.send_str(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": "Operation accepted. Thank the user and end the call.",
                },
            }))
            return
        if name == "decline_operation":
            logger.info(
                "[phone_verify.tool] decline_operation: call_id=%s", call_id,
            )
            verifier.resolve(call_id, False)
            await server_ws.send_str(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": "Operation declined. Thank the user and end the call.",
                },
            }))
            return
        await original_execute(item, server_ws)

    middleware._execute_tool = patched_execute

    def cleanup() -> None:
        if middleware._execute_tool is patched_execute:
            middleware._execute_tool = original_execute
            logger.debug("[phone_verify] restored original _execute_tool")
        else:
            logger.debug(
                "[phone_verify] _execute_tool already replaced, skipping restore",
            )

    return cleanup

VERIFY_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "accept_operation",
        "description": "Accept the pending tool operation after the user confirms.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "decline_operation",
        "description": "Decline the pending tool operation after the user refuses.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]
