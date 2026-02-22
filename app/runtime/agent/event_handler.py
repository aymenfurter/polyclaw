"""Copilot SDK session event handler.

Dispatch table replaces deep if/elif chains for session events.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from copilot.generated.session_events import SessionEventType

logger = logging.getLogger(__name__)


class EventHandler:
    """Callable that dispatches Copilot SDK session events."""

    def __init__(
        self,
        on_delta: Callable[[str], None] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.on_delta = on_delta
        self.on_event = on_event
        self.final_text: str | None = None
        self.error: str | None = None
        self.done = asyncio.Event()
        self.event_count: int = 0
        self._tool_names: dict[str, str] = {}
        self._seen_tool_starts: set[str] = set()
        self._seen_tool_completes: set[str] = set()
        # Token usage tracking -- populated from SDK events when available.
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None

    _QUIET_EVENT_TYPES: frozenset[str] = frozenset({
        "SessionEventType.ASSISTANT_MESSAGE_DELTA",
        "SessionEventType.ASSISTANT_REASONING_DELTA",
    })

    def __call__(self, event: Any) -> None:
        etype = event.type
        self.event_count += 1
        if str(etype) not in self._QUIET_EVENT_TYPES:
            logger.info("[event_handler] event #%d type=%s", self.event_count, etype)
        if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            self._handle_delta(event)
        elif etype == SessionEventType.ASSISTANT_MESSAGE:
            self.final_text = event.data.content
            logger.info("[event_handler] ASSISTANT_MESSAGE received, len=%d", len(self.final_text or ""))
            self._extract_usage(event)
        elif etype == SessionEventType.SESSION_IDLE:
            logger.info("[event_handler] SESSION_IDLE -- marking done")
            self.done.set()
        elif etype == SessionEventType.SESSION_ERROR:
            self.error = str(event.data) if hasattr(event, "data") else "Unknown error"
            logger.error("[event_handler] SESSION_ERROR: %s", self.error)
            self.done.set()
        elif self.on_event:
            self._dispatch_intermediate(etype, event)

    def _handle_delta(self, event: Any) -> None:
        chunk = event.data.delta_content or ""
        if self.on_delta and chunk:
            self.on_delta(chunk)

    def _extract_usage(self, event: Any) -> None:
        """Extract token usage from the ASSISTANT_MESSAGE event if available."""
        data = event.data if hasattr(event, "data") else None
        if not data:
            return
        usage = getattr(data, "usage", None)
        if usage:
            self.input_tokens = getattr(usage, "input_tokens", None) or getattr(
                usage, "prompt_tokens", None
            )
            self.output_tokens = getattr(usage, "output_tokens", None) or getattr(
                usage, "completion_tokens", None
            )
            if self.input_tokens is not None or self.output_tokens is not None:
                logger.info(
                    "[event_handler] token usage: input=%s output=%s",
                    self.input_tokens,
                    self.output_tokens,
                )

    def _dispatch_intermediate(self, etype: Any, event: Any) -> None:
        handler = _DISPATCH_TABLE.get(etype)
        if handler:
            handler(self, etype, event)

    def _on_tool_start(self, _etype: Any, event: Any) -> None:
        assert self.on_event is not None
        tool = _extract_tool_name(event.data)
        call_id = event.data.tool_call_id or ""
        if call_id:
            # Skip duplicate TOOL_EXECUTION_START events for the same call_id.
            # The SDK can fire this event more than once per tool invocation.
            if call_id in self._seen_tool_starts:
                logger.debug("[event_handler] skipping duplicate tool_start: call_id=%s", call_id)
                return
            self._seen_tool_starts.add(call_id)
            self._tool_names[call_id] = tool
        self.on_event("tool_start", {
            "tool": tool,
            "call_id": call_id,
            "arguments": _serialize_arguments(event.data.arguments),
            "mcp_server": getattr(event.data, "mcp_server_name", None) or "",
        })

    def _on_tool_complete(self, _etype: Any, event: Any) -> None:
        assert self.on_event is not None
        call_id = event.data.tool_call_id or ""
        if call_id:
            if call_id in self._seen_tool_completes:
                logger.debug("[event_handler] skipping duplicate tool_done: call_id=%s", call_id)
                return
            self._seen_tool_completes.add(call_id)
        tool = _extract_tool_name(event.data, self._tool_names.get(call_id, "unknown"))
        result_text = None
        if event.data.result and event.data.result.content:
            result_text = event.data.result.content[:500]
        self.on_event("tool_done", {"tool": tool, "call_id": call_id, "result": result_text})

    def _on_tool_progress(self, _etype: Any, event: Any) -> None:
        assert self.on_event is not None
        call_id = event.data.tool_call_id or ""
        tool = _extract_tool_name(event.data, self._tool_names.get(call_id, "unknown"))
        self.on_event("tool_progress", {
            "tool": tool,
            "call_id": call_id,
            "message": event.data.progress_message or "",
        })

    def _on_reasoning(self, _etype: Any, event: Any) -> None:
        assert self.on_event is not None
        text = event.data.reasoning_text or event.data.delta_content or ""
        if text:
            self.on_event("reasoning", {"text": text})

    def _on_skill(self, _etype: Any, event: Any) -> None:
        assert self.on_event is not None
        skill_name = event.data.name or "unknown"
        self.on_event("skill", {"name": skill_name})
        from ..state.profile import increment_skill_usage
        increment_skill_usage(skill_name)

    def _on_subagent_start(self, _etype: Any, event: Any) -> None:
        assert self.on_event is not None
        name = event.data.agent_name or event.data.agent_display_name or "unknown"
        self.on_event("subagent_start", {"name": name})

    def _on_subagent_done(self, _etype: Any, event: Any) -> None:
        assert self.on_event is not None
        name = event.data.agent_name or event.data.agent_display_name or "unknown"
        self.on_event("subagent_done", {"name": name})


_DISPATCH_TABLE: dict[Any, Callable] = {
    SessionEventType.TOOL_EXECUTION_START: EventHandler._on_tool_start,
    SessionEventType.TOOL_EXECUTION_COMPLETE: EventHandler._on_tool_complete,
    SessionEventType.TOOL_EXECUTION_PROGRESS: EventHandler._on_tool_progress,
    SessionEventType.ASSISTANT_REASONING_DELTA: EventHandler._on_reasoning,
    SessionEventType.SKILL_INVOKED: EventHandler._on_skill,
    SessionEventType.SUBAGENT_STARTED: EventHandler._on_subagent_start,
    SessionEventType.SUBAGENT_COMPLETED: EventHandler._on_subagent_done,
}


def _extract_tool_name(data: Any, fallback: str = "unknown") -> str:
    return data.tool_name or getattr(data, "mcp_tool_name", None) or data.name or fallback


def _serialize_arguments(args: Any) -> str | None:
    """Normalize tool arguments to a JSON string."""
    if args is None:
        return None
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args)
    except (TypeError, ValueError):
        return str(args)
