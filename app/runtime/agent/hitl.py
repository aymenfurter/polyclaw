"""Human-in-the-loop tool approval interceptor."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..state.guardrails import GuardrailsConfigStore
from .hitl_channels import (
    apply_aitl_review,
    apply_filter_check,
    ask_bot_approval,
    ask_chat_approval,
    ask_phone_approval,
)

if TYPE_CHECKING:
    from ..services.security.prompt_shield import PromptShieldService
    from ..state.tool_activity_store import ToolActivityStore
    from .aitl import AitlReviewer
    from .phone_verify import PhoneVerifier

logger = logging.getLogger(__name__)

_ALWAYS_APPROVED_TOOLS: frozenset[str] = frozenset({"report_intent"})

_ALLOW: dict[str, str] = {"permissionDecision": "allow"}
_DENY: dict[str, str] = {"permissionDecision": "deny"}


class HitlInterceptor:
    """Human-in-the-loop tool approval interceptor.

    Per-turn state (emit, model, session context) is bound via
    :meth:`bind_turn` and released via :meth:`unbind_turn`.  Persistent
    wiring (phone verifier, AITL reviewer, prompt shield) is set once
    during application startup.
    """

    def __init__(self, guardrails: GuardrailsConfigStore) -> None:
        self._guardrails = guardrails

        # -- per-turn state (bound/unbound each agent turn) ----------------
        self._emit: Callable[[str, dict[str, Any]], None] | None = None
        self._bot_reply_fn: Callable[[str], Awaitable[None]] | None = None
        self._execution_context: str = ""
        self._model: str = ""
        self._session_id: str = ""
        self._tool_activity: ToolActivityStore | None = None

        # -- persistent state ----------------------------------------------
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._phone_verifier: PhoneVerifier | None = None
        self._aitl_reviewer: AitlReviewer | None = None
        self._prompt_shield: PromptShieldService | None = None
        self._resolved_strategies: dict[str, list[str]] = {}
        self._last_shield_result: dict[str, Any] | None = None

    # -- per-turn lifecycle ------------------------------------------------

    def bind_turn(
        self,
        *,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        bot_reply_fn: Callable[[str], Awaitable[None]] | None = None,
        execution_context: str = "",
        model: str = "",
        session_id: str = "",
        tool_activity: ToolActivityStore | None = None,
    ) -> None:
        """Bind per-turn state before an agent send."""
        self._emit = emit
        self._bot_reply_fn = bot_reply_fn
        self._execution_context = execution_context
        self._model = model
        self._session_id = session_id
        self._tool_activity = tool_activity

    def unbind_turn(self) -> None:
        """Clear per-turn state after an agent send completes."""
        self._emit = None
        self._bot_reply_fn = None
        self._execution_context = ""
        self._model = ""
        self._session_id = ""
        self._tool_activity = None

    # -- persistent wiring -------------------------------------------------

    def set_phone_verifier(self, verifier: PhoneVerifier) -> None:
        self._phone_verifier = verifier

    def set_aitl_reviewer(self, reviewer: AitlReviewer) -> None:
        self._aitl_reviewer = reviewer

    def set_prompt_shield(self, shield: PromptShieldService) -> None:
        self._prompt_shield = shield

    def pop_resolved_strategy(self, tool_name: str) -> str:
        queue = self._resolved_strategies.get(tool_name)
        if not queue:
            return ""
        strategy = queue.pop(0)
        if not queue:
            del self._resolved_strategies[tool_name]
        return strategy

    def _record_denied(
        self,
        tool_name: str,
        call_id: str,
        args_str: str,
        interaction_type: str,
    ) -> None:
        if not self._tool_activity:
            return
        entry = self._tool_activity.record_start(
            session_id=self._session_id,
            tool=tool_name,
            call_id=call_id,
            arguments=args_str,
            model=self._model,
            interaction_type=interaction_type,
        )
        if self._last_shield_result:
            self._tool_activity.update_shield_result(
                call_id=call_id,
                shield_result=self._last_shield_result.get("result", ""),
                shield_detail=self._last_shield_result.get("detail", ""),
                shield_elapsed_ms=self._last_shield_result.get("elapsed_ms"),
            )
        self._tool_activity.record_complete(
            call_id=call_id,
            result="Denied by guardrail",
            status="denied",
        )
        logger.info(
            "[hitl.record] recorded denied tool: id=%s tool=%s itl=%s",
            entry.id, tool_name, interaction_type,
        )

    async def on_pre_tool_use(self, input_data: dict, invocation: Any) -> dict:
        tool_name = input_data.get("toolName") or getattr(invocation, "name", None) or "unknown"

        if tool_name in _ALWAYS_APPROVED_TOOLS:
            logger.info("[hitl.hook] ALLOW (always-approved) tool=%s", tool_name)
            return {"permissionDecision": "allow"}

        result = await self._evaluate_tool(input_data, tool_name)
        if result["permissionDecision"] == "deny":
            call_id = input_data.get("toolCallId") or str(uuid.uuid4())[:8]
            args_str = str(input_data.get("toolArgs") or input_data.get("input", ""))
            if len(args_str) > 500:
                args_str = args_str[:497] + "..."
            queue = self._resolved_strategies.get(tool_name, [])
            itl = queue[-1] if queue else "deny"
            self._record_denied(tool_name, call_id, args_str, itl)
        return result

    async def _evaluate_tool(self, input_data: dict, tool_name: str) -> dict:
        """Evaluate a tool invocation against the guardrails policy."""
        self._last_shield_result = None

        call_id = input_data.get("toolCallId") or str(uuid.uuid4())[:8]
        mcp_server = input_data.get("mcpServerName") or ""
        args_str = str(input_data.get("toolArgs") or input_data.get("input", ""))
        if len(args_str) > 500:
            args_str = args_str[:497] + "..."

        logger.info(
            "[hitl.hook] ENTER on_pre_tool_use: tool=%s call_id=%s "
            "ctx=%s model=%s mcp=%s hitl_enabled=%s",
            tool_name, call_id, self._execution_context,
            self._model, mcp_server or "(none)",
            self._guardrails.hitl_enabled,
        )

        strategy = self._guardrails.resolve_action(
            tool_name,
            mcp_server=mcp_server or None,
            execution_context=self._execution_context,
            model=self._model,
        )
        logger.info(
            "[hitl.hook] resolved strategy=%s for tool=%s",
            strategy, tool_name,
        )

        # Terminal strategies
        if strategy == "allow":
            logger.info("[hitl.hook] ALLOW tool=%s call_id=%s", tool_name, call_id)
            return _ALLOW

        if strategy == "deny":
            return self._make_deny(call_id, tool_name)

        # Pre-filter: run Prompt Shield before non-filter strategies
        if (
            self._prompt_shield
            and self._prompt_shield.configured
            and strategy != "filter"
        ):
            shield_result = await self._apply_filter(call_id, tool_name, args_str)
            if shield_result is not None:
                logger.info(
                    "[hitl.hook] Prompt Shield pre-check DENIED tool=%s "
                    "before strategy=%s",
                    tool_name, strategy,
                )
                return shield_result

        # Strategy-specific handler
        result = await self._dispatch_strategy(
            strategy, call_id, tool_name, args_str,
        )
        if result is not None:
            return result

        # Fallback: interactive approval
        return await self._route_interactive(
            call_id, tool_name, args_str, mcp_server,
        )

    def _make_deny(self, call_id: str, tool_name: str) -> dict:
        """Build a deny response and emit an event."""
        logger.info("[hitl.hook] DENY tool=%s call_id=%s", tool_name, call_id)
        self._resolved_strategies.setdefault(tool_name, []).append("deny")
        if self._emit:
            self._emit("tool_denied", {
                "call_id": call_id,
                "tool": tool_name,
                "reason": "Denied by guardrail rule",
            })
        return dict(_DENY)

    async def _dispatch_strategy(
        self,
        strategy: str,
        call_id: str,
        tool_name: str,
        args_str: str,
    ) -> dict | None:
        """Delegate to a strategy-specific handler.

        Returns a decision dict, or ``None`` to fall through to
        interactive approval.
        """
        if strategy == "aitl":
            return await self._handle_aitl(call_id, tool_name, args_str)
        if strategy == "filter":
            return await self._handle_filter(call_id, tool_name, args_str)
        if strategy == "pitl":
            return await self._handle_pitl(call_id, tool_name, args_str)
        return None

    async def _handle_aitl(
        self, call_id: str, tool_name: str, args_str: str,
    ) -> dict | None:
        """AI-in-the-loop review."""
        self._resolved_strategies.setdefault(tool_name, []).append("aitl")
        if self._aitl_reviewer:
            return await self._apply_aitl(call_id, tool_name, args_str)
        logger.warning(
            "[hitl] AITL requested but unavailable, "
            "falling back to interactive: tool=%s",
            tool_name,
        )
        return None

    async def _handle_filter(
        self, call_id: str, tool_name: str, args_str: str,
    ) -> dict | None:
        """Content-safety filter."""
        self._resolved_strategies.setdefault(tool_name, []).append("filter")
        if self._prompt_shield:
            result = await self._apply_filter(call_id, tool_name, args_str)
            if result is not None:
                return result
            logger.info(
                "[hitl.hook] shield passed, ALLOW tool=%s call_id=%s",
                tool_name, call_id,
            )
            return dict(_ALLOW)
        logger.warning(
            "[hitl] no prompt shield available, allowing tool=%s "
            "(Content Safety not deployed)",
            tool_name,
        )
        self._last_shield_result = {
            "result": "skipped",
            "detail": "Content Safety not deployed",
            "elapsed_ms": None,
        }
        return dict(_ALLOW)

    async def _handle_pitl(
        self, call_id: str, tool_name: str, args_str: str,
    ) -> dict | None:
        """Phone-in-the-loop verification."""
        self._resolved_strategies.setdefault(tool_name, []).append("pitl")
        if self._phone_verifier:
            logger.info("[hitl.hook] PITL routing to phone: tool=%s", tool_name)
            return await self._ask_phone(call_id, tool_name, args_str)
        logger.warning(
            "[hitl] PITL requested but phone verifier unavailable, "
            "falling back to chat: tool=%s",
            tool_name,
        )
        return None

    async def _route_interactive(
        self,
        call_id: str,
        tool_name: str,
        args_str: str,
        mcp_server: str,
    ) -> dict:
        """Route to the best available interactive approval channel."""
        logger.info(
            "[hitl.hook] interactive approval needed: tool=%s "
            "has_emit=%s has_bot_reply=%s has_phone=%s",
            tool_name,
            self._emit is not None,
            self._bot_reply_fn is not None,
            self._phone_verifier is not None,
        )
        channel = self._guardrails.resolve_channel(
            tool_name,
            mcp_server=mcp_server or None,
            execution_context=self._execution_context,
            model=self._model,
        )

        if channel == "phone" and self._phone_verifier:
            logger.info(
                "[hitl.hook] routing to phone channel: tool=%s", tool_name,
            )
            self._resolved_strategies.setdefault(tool_name, []).append("pitl")
            return await self._ask_phone(call_id, tool_name, args_str)

        if self._bot_reply_fn:
            logger.info(
                "[hitl.hook] routing to bot channel: tool=%s", tool_name,
            )
            self._resolved_strategies.setdefault(tool_name, []).append("hitl")
            return await self._ask_bot_channel(call_id, tool_name, args_str)

        if self._emit:
            logger.info(
                "[hitl.hook] routing to web chat: tool=%s", tool_name,
            )
            self._resolved_strategies.setdefault(tool_name, []).append("hitl")
            return await self._ask_chat(call_id, tool_name, args_str)

        logger.error(
            "[hitl.hook] NO APPROVAL CHANNEL available -- "
            "denying tool=%s call_id=%s to avoid silent hang",
            tool_name, call_id,
        )
        return dict(_DENY)

    def resolve_approval(self, call_id: str, approved: bool) -> bool:
        future = self._pending.get(call_id)
        if future and not future.done():
            future.set_result(approved)
            logger.info("[hitl] approval resolved: call_id=%s approved=%s", call_id, approved)
            return True
        logger.warning("[hitl] no pending approval for call_id=%s", call_id)
        return False

    def resolve_bot_reply(self, text: str) -> bool:
        if not self._pending:
            return False
        call_id = next(iter(self._pending))
        approved = text.strip().lower() in ("y", "yes")
        return self.resolve_approval(call_id, approved)

    @property
    def has_pending_approval(self) -> bool:
        return bool(self._pending)

    async def _ask_chat(self, call_id: str, tool_name: str, args_str: str) -> dict:
        if not self._emit:
            logger.error(
                "[hitl.chat] _ask_chat called with no emitter -- "
                "denying tool=%s immediately", tool_name,
            )
            return {"permissionDecision": "deny"}
        return await ask_chat_approval(
            emit=self._emit,
            pending=self._pending,
            call_id=call_id,
            tool_name=tool_name,
            args_str=args_str,
        )

    async def _ask_bot_channel(
        self, call_id: str, tool_name: str, args_str: str,
    ) -> dict:
        assert self._bot_reply_fn is not None
        return await ask_bot_approval(
            bot_reply_fn=self._bot_reply_fn,
            pending=self._pending,
            call_id=call_id,
            tool_name=tool_name,
            args_str=args_str,
        )

    async def _ask_phone(self, call_id: str, tool_name: str, args_str: str) -> dict:
        assert self._phone_verifier is not None
        return await ask_phone_approval(
            phone_verifier=self._phone_verifier,
            emit=self._emit,
            call_id=call_id,
            tool_name=tool_name,
            args_str=args_str,
        )

    async def _apply_aitl(self, call_id: str, tool_name: str, args_str: str) -> dict | None:
        assert self._aitl_reviewer is not None
        return await apply_aitl_review(
            aitl_reviewer=self._aitl_reviewer,
            emit=self._emit,
            call_id=call_id,
            tool_name=tool_name,
            args_str=args_str,
        )

    async def _apply_filter(self, call_id: str, tool_name: str, args_str: str) -> dict | None:
        assert self._prompt_shield is not None
        decision, shield_info = await apply_filter_check(
            prompt_shield=self._prompt_shield,
            tool_activity=self._tool_activity,
            emit=self._emit,
            call_id=call_id,
            tool_name=tool_name,
            args_str=args_str,
        )
        self._last_shield_result = shield_info
        return decision
