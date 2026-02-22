"""Human-in-the-loop tool approval interceptor."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..state.guardrails_config import GuardrailsConfigStore
from ..util.async_helpers import run_sync

if TYPE_CHECKING:
    from ..services.prompt_shield import PromptShieldService
    from ..state.tool_activity_store import ToolActivityStore
    from .aitl import AitlReviewer
    from .phone_verify import PhoneVerifier

logger = logging.getLogger(__name__)

_APPROVAL_TIMEOUT = 300.0

_ALWAYS_APPROVED_TOOLS: frozenset[str] = frozenset({"report_intent"})


class HitlInterceptor:

    def __init__(self, guardrails: GuardrailsConfigStore) -> None:
        self._guardrails = guardrails
        self._emit: Callable[[str, dict[str, Any]], None] | None = None
        self._bot_reply_fn: Callable[[str], Awaitable[None]] | None = None
        self._execution_context: str = ""
        self._model: str = ""
        self._session_id: str = ""
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._phone_verifier: PhoneVerifier | None = None
        self._aitl_reviewer: AitlReviewer | None = None
        self._prompt_shield: PromptShieldService | None = None
        self._tool_activity: ToolActivityStore | None = None
        self._resolved_strategies: dict[str, list[str]] = {}
        self._last_shield_result: dict[str, Any] | None = None

    def set_emit(self, emit: Callable[[str, dict[str, Any]], None]) -> None:
        self._emit = emit

    def clear_emit(self) -> None:
        self._emit = None

    def set_bot_reply_fn(self, fn: Callable[[str], Awaitable[None]]) -> None:
        self._bot_reply_fn = fn

    def clear_bot_reply_fn(self) -> None:
        self._bot_reply_fn = None

    def set_execution_context(self, context: str) -> None:
        self._execution_context = context

    def set_model(self, model: str) -> None:
        self._model = model

    def set_phone_verifier(self, verifier: PhoneVerifier) -> None:
        self._phone_verifier = verifier

    def set_aitl_reviewer(self, reviewer: AitlReviewer) -> None:
        self._aitl_reviewer = reviewer

    def set_prompt_shield(self, shield: PromptShieldService) -> None:
        self._prompt_shield = shield

    def set_tool_activity(self, store: ToolActivityStore) -> None:
        self._tool_activity = store

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

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
        logger.info(
            "[hitl.hook] input_data keys=%s",
            list(input_data.keys()),
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

        if strategy == "allow":
            logger.info("[hitl.hook] ALLOW tool=%s call_id=%s", tool_name, call_id)
            return {"permissionDecision": "allow"}

        if strategy == "deny":
            logger.info("[hitl.hook] DENY tool=%s call_id=%s", tool_name, call_id)
            self._resolved_strategies.setdefault(tool_name, []).append("deny")
            if self._emit:
                self._emit("tool_denied", {
                    "call_id": call_id,
                    "tool": tool_name,
                    "reason": "Denied by guardrail rule",
                })
            return {"permissionDecision": "deny"}

        if self._prompt_shield and self._prompt_shield.configured and strategy != "filter":
            shield_result = await self._apply_filter(call_id, tool_name, args_str)
            if shield_result is not None:
                logger.info(
                    "[hitl.hook] Prompt Shield pre-check DENIED tool=%s "
                    "before strategy=%s",
                    tool_name, strategy,
                )
                return shield_result

        if strategy == "aitl":
            self._resolved_strategies.setdefault(tool_name, []).append("aitl")
            if self._aitl_reviewer:
                result = await self._apply_aitl(call_id, tool_name, args_str)
                if result is not None:
                    return result
            logger.warning(
                "[hitl] AITL requested but unavailable, falling back to interactive: tool=%s",
                tool_name,
            )

        if strategy == "filter":
            self._resolved_strategies.setdefault(tool_name, []).append("filter")
            if self._prompt_shield:
                result = await self._apply_filter(call_id, tool_name, args_str)
                if result is not None:
                    return result
                logger.info(
                    "[hitl.hook] shield passed, ALLOW tool=%s call_id=%s",
                    tool_name, call_id,
                )
                return {"permissionDecision": "allow"}
            logger.warning(
                "[hitl] no prompt shield available, allowing tool=%s (Content Safety not deployed)",
                tool_name,
            )
            self._last_shield_result = {
                "result": "skipped",
                "detail": "Content Safety not deployed",
                "elapsed_ms": None,
            }
            return {"permissionDecision": "allow"}

        if strategy == "pitl":
            self._resolved_strategies.setdefault(tool_name, []).append("pitl")
            if self._phone_verifier:
                logger.info("[hitl.hook] PITL routing to phone: tool=%s", tool_name)
                return await self._ask_phone(call_id, tool_name, args_str)
            logger.warning(
                "[hitl] PITL requested but phone verifier unavailable, "
                "falling back to chat: tool=%s", tool_name,
            )

        logger.info(
            "[hitl.hook] interactive approval needed: tool=%s strategy=%s "
            "has_emit=%s has_bot_reply=%s has_phone=%s",
            tool_name, strategy,
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
            logger.info("[hitl.hook] routing to phone channel: tool=%s", tool_name)
            self._resolved_strategies.setdefault(tool_name, []).append("pitl")
            return await self._ask_phone(call_id, tool_name, args_str)

        if self._bot_reply_fn:
            logger.info("[hitl.hook] routing to bot channel: tool=%s", tool_name)
            self._resolved_strategies.setdefault(tool_name, []).append("hitl")
            return await self._ask_bot_channel(call_id, tool_name, args_str)

        if self._emit:
            logger.info("[hitl.hook] routing to web chat: tool=%s", tool_name)
            self._resolved_strategies.setdefault(tool_name, []).append("hitl")
            return await self._ask_chat(call_id, tool_name, args_str)

        logger.error(
            "[hitl.hook] NO APPROVAL CHANNEL available (no bot_reply_fn, "
            "no emit) -- denying tool=%s call_id=%s to avoid silent hang",
            tool_name, call_id,
        )
        return {"permissionDecision": "deny"}

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

        logger.info(
            "[hitl.chat] sending approval_request via WebSocket: "
            "tool=%s call_id=%s",
            tool_name, call_id,
        )
        self._emit("approval_request", {
            "call_id": call_id,
            "tool": tool_name,
            "arguments": args_str,
        })
        logger.info("[hitl.chat] approval_request emitted, waiting for response...")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[call_id] = future

        try:
            approved = await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[hitl] approval timed out: call_id=%s tool=%s", call_id, tool_name)
            approved = False
        finally:
            self._pending.pop(call_id, None)

        decision = "allow" if approved else "deny"
        logger.info(
            "[hitl.chat] decision: tool=%s call_id=%s approved=%s decision=%s",
            tool_name, call_id, approved, decision,
        )
        if self._emit:
            self._emit("approval_resolved", {
                "call_id": call_id,
                "tool": tool_name,
                "approved": approved,
            })
        return {"permissionDecision": decision}

    async def _ask_bot_channel(
        self, call_id: str, tool_name: str, args_str: str,
    ) -> dict:
        assert self._bot_reply_fn is not None
        truncated = args_str if len(args_str) <= 200 else args_str[:197] + "..."
        confirmation_msg = (
            f"The agent wants to use the tool **{tool_name}**.\n\n"
            f"Arguments: `{truncated}`\n\n"
            f"Reply **y** to approve or anything else to deny."
        )
        logger.info(
            "[hitl] bot-channel approval request: tool=%s call_id=%s",
            tool_name, call_id,
        )
        try:
            await self._bot_reply_fn(confirmation_msg)
        except Exception:
            logger.exception("[hitl] failed to send bot approval message: call_id=%s", call_id)
            return {"permissionDecision": "deny"}

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[call_id] = future

        try:
            approved = await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[hitl] bot approval timed out: call_id=%s tool=%s", call_id, tool_name)
            approved = False
        finally:
            self._pending.pop(call_id, None)

        decision = "allow" if approved else "deny"
        logger.info(
            "[hitl] bot-channel decision: tool=%s call_id=%s decision=%s",
            tool_name, call_id, decision,
        )

        outcome_msg = (
            f"Tool **{tool_name}** {'approved' if approved else 'denied'}."
        )
        try:
            await self._bot_reply_fn(outcome_msg)
        except Exception:
            logger.exception("[hitl] failed to send bot outcome message: call_id=%s", call_id)

        return {"permissionDecision": decision}

    async def _ask_phone(self, call_id: str, tool_name: str, args_str: str) -> dict:
        assert self._phone_verifier is not None
        logger.info("[hitl] phone verification: tool=%s call_id=%s", tool_name, call_id)

        if self._emit:
            self._emit("phone_verification_started", {
                "call_id": call_id,
                "tool": tool_name,
                "arguments": args_str,
            })

        try:
            approved = await self._phone_verifier.request_verification(
                call_id=call_id,
                tool_name=tool_name,
                tool_args=args_str,
            )
        except Exception:
            logger.exception("[hitl] phone verification failed: call_id=%s", call_id)
            approved = False

        decision = "allow" if approved else "deny"
        logger.info("[hitl] phone decision: tool=%s call_id=%s decision=%s", tool_name, call_id, decision)

        if self._emit:
            self._emit("phone_verification_complete", {
                "call_id": call_id,
                "tool": tool_name,
                "approved": approved,
            })

        return {"permissionDecision": decision}

    async def _apply_aitl(self, call_id: str, tool_name: str, args_str: str) -> dict | None:
        assert self._aitl_reviewer is not None
        if self._emit:
            self._emit("aitl_review_started", {
                "call_id": call_id,
                "tool": tool_name,
            })
        try:
            approved, reason = await self._aitl_reviewer.review(
                tool_name=tool_name,
                arguments=args_str,
            )
        except Exception:
            logger.exception("[hitl] AITL review error: call_id=%s", call_id)
            return None

        if self._emit:
            self._emit("aitl_review_complete", {
                "call_id": call_id,
                "tool": tool_name,
                "approved": approved,
                "reason": reason,
            })

        decision = "allow" if approved else "deny"
        logger.info(
            "[hitl] AITL decision: tool=%s call_id=%s decision=%s reason=%s",
            tool_name, call_id, decision, reason,
        )
        return {"permissionDecision": decision}

    async def _apply_filter(self, call_id: str, tool_name: str, args_str: str) -> dict | None:
        assert self._prompt_shield is not None
        import time as _time

        t0 = _time.monotonic()
        try:
            result = await run_sync(self._prompt_shield.check, args_str)
        except Exception:
            elapsed_ms = (_time.monotonic() - t0) * 1000
            logger.exception("[hitl] Prompt Shield error: call_id=%s", call_id)
            self._last_shield_result = {
                "result": "error",
                "detail": "Shield check raised an exception",
                "elapsed_ms": round(elapsed_ms, 1),
            }
            if self._tool_activity:
                self._tool_activity.update_shield_result(
                    call_id=call_id, shield_result="error",
                    shield_detail="Shield check raised an exception",
                    shield_elapsed_ms=round(elapsed_ms, 1),
                )
            return None

        elapsed_ms = (_time.monotonic() - t0) * 1000
        shield_status = "attack" if result.attack_detected else "clean"
        self._last_shield_result = {
            "result": shield_status,
            "detail": result.detail,
            "elapsed_ms": round(elapsed_ms, 1),
        }

        if self._tool_activity:
            self._tool_activity.update_shield_result(
                call_id=call_id,
                shield_result=shield_status,
                shield_detail=result.detail,
                shield_elapsed_ms=round(elapsed_ms, 1),
            )

        if result.attack_detected:
            logger.info(
                "[hitl] Prompt Shield denied: tool=%s call_id=%s detail=%s elapsed=%.0fms",
                tool_name, call_id, result.detail, elapsed_ms,
            )
            if self._emit:
                self._emit("tool_denied", {
                    "call_id": call_id,
                    "tool": tool_name,
                    "reason": "Blocked by content filter",
                    "shield_detail": result.detail,
                })
            return {"permissionDecision": "deny"}

        logger.info(
            "[hitl] Prompt Shield passed: tool=%s call_id=%s elapsed=%.0fms",
            tool_name, call_id, elapsed_ms,
        )
        return None
