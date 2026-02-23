"""Approval-channel implementations for the HITL interceptor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..util.async_helpers import run_sync

if TYPE_CHECKING:
    from ..services.security.prompt_shield import PromptShieldService
    from ..state.tool_activity_store import ToolActivityStore
    from .aitl import AitlReviewer
    from .phone_verify import PhoneVerifier

logger = logging.getLogger(__name__)

_APPROVAL_TIMEOUT = 300.0


async def ask_chat_approval(
    *,
    emit: Callable[[str, dict[str, Any]], None],
    pending: dict[str, asyncio.Future[bool]],
    call_id: str,
    tool_name: str,
    args_str: str,
    timeout: float = _APPROVAL_TIMEOUT,
) -> dict[str, str]:
    """Request approval via the WebSocket chat channel."""
    logger.info(
        "[hitl.chat] sending approval_request via WebSocket: "
        "tool=%s call_id=%s",
        tool_name, call_id,
    )
    emit("approval_request", {
        "call_id": call_id,
        "tool": tool_name,
        "arguments": args_str,
    })
    logger.info("[hitl.chat] approval_request emitted, waiting for response...")

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    pending[call_id] = future

    try:
        approved = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[hitl] approval timed out: call_id=%s tool=%s", call_id, tool_name)
        approved = False
    finally:
        pending.pop(call_id, None)

    decision = "allow" if approved else "deny"
    logger.info(
        "[hitl.chat] decision: tool=%s call_id=%s approved=%s decision=%s",
        tool_name, call_id, approved, decision,
    )
    emit("approval_resolved", {
        "call_id": call_id,
        "tool": tool_name,
        "approved": approved,
    })
    return {"permissionDecision": decision}


async def ask_bot_approval(
    *,
    bot_reply_fn: Callable[[str], Awaitable[None]],
    pending: dict[str, asyncio.Future[bool]],
    call_id: str,
    tool_name: str,
    args_str: str,
    timeout: float = _APPROVAL_TIMEOUT,
) -> dict[str, str]:
    """Request approval via a messaging-bot reply channel."""
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
        await bot_reply_fn(confirmation_msg)
    except Exception:
        logger.exception("[hitl] failed to send bot approval message: call_id=%s", call_id)
        return {"permissionDecision": "deny"}

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bool] = loop.create_future()
    pending[call_id] = future

    try:
        approved = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[hitl] bot approval timed out: call_id=%s tool=%s", call_id, tool_name)
        approved = False
    finally:
        pending.pop(call_id, None)

    decision = "allow" if approved else "deny"
    logger.info(
        "[hitl] bot-channel decision: tool=%s call_id=%s decision=%s",
        tool_name, call_id, decision,
    )

    outcome_msg = (
        f"Tool **{tool_name}** {'approved' if approved else 'denied'}."
    )
    try:
        await bot_reply_fn(outcome_msg)
    except Exception:
        logger.exception("[hitl] failed to send bot outcome message: call_id=%s", call_id)

    return {"permissionDecision": decision}


async def ask_phone_approval(
    *,
    phone_verifier: PhoneVerifier,
    emit: Callable[[str, dict[str, Any]], None] | None,
    call_id: str,
    tool_name: str,
    args_str: str,
) -> dict[str, str]:
    """Request approval via phone verification."""
    logger.info("[hitl] phone verification: tool=%s call_id=%s", tool_name, call_id)

    if emit:
        emit("phone_verification_started", {
            "call_id": call_id,
            "tool": tool_name,
            "arguments": args_str,
        })

    try:
        approved = await phone_verifier.request_verification(
            call_id=call_id,
            tool_name=tool_name,
            tool_args=args_str,
        )
    except Exception:
        logger.exception("[hitl] phone verification failed: call_id=%s", call_id)
        approved = False

    decision = "allow" if approved else "deny"
    logger.info("[hitl] phone decision: tool=%s call_id=%s decision=%s", tool_name, call_id, decision)

    if emit:
        emit("phone_verification_complete", {
            "call_id": call_id,
            "tool": tool_name,
            "approved": approved,
        })

    return {"permissionDecision": decision}


async def apply_aitl_review(
    *,
    aitl_reviewer: AitlReviewer,
    emit: Callable[[str, dict[str, Any]], None] | None,
    call_id: str,
    tool_name: str,
    args_str: str,
) -> dict[str, str] | None:
    """Run an AI-in-the-loop review. Returns decision or ``None`` on error."""
    if emit:
        emit("aitl_review_started", {
            "call_id": call_id,
            "tool": tool_name,
        })
    try:
        approved, reason = await aitl_reviewer.review(
            tool_name=tool_name,
            arguments=args_str,
        )
    except Exception:
        logger.exception("[hitl] AITL review error: call_id=%s", call_id)
        return None

    if emit:
        emit("aitl_review_complete", {
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


async def apply_filter_check(
    *,
    prompt_shield: PromptShieldService,
    tool_activity: ToolActivityStore | None,
    emit: Callable[[str, dict[str, Any]], None] | None,
    call_id: str,
    tool_name: str,
    args_str: str,
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    """Run a Prompt Shield content-safety check.

    Returns ``(decision | None, shield_result_info)``.  When ``decision``
    is ``None`` the content passed the filter and the caller should
    continue with the next step.
    """
    import time as _time

    t0 = _time.monotonic()
    try:
        result = await run_sync(prompt_shield.check, args_str)
    except Exception:
        elapsed_ms = (_time.monotonic() - t0) * 1000
        logger.exception("[hitl] Prompt Shield error: call_id=%s", call_id)
        shield_info: dict[str, Any] = {
            "result": "error",
            "detail": "Shield check raised an exception",
            "elapsed_ms": round(elapsed_ms, 1),
        }
        if tool_activity:
            tool_activity.update_shield_result(
                call_id=call_id, shield_result="error",
                shield_detail="Shield check raised an exception",
                shield_elapsed_ms=round(elapsed_ms, 1),
            )
        return None, shield_info

    elapsed_ms = (_time.monotonic() - t0) * 1000
    shield_status = "attack" if result.attack_detected else "clean"
    shield_info = {
        "result": shield_status,
        "detail": result.detail,
        "elapsed_ms": round(elapsed_ms, 1),
    }

    if tool_activity:
        tool_activity.update_shield_result(
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
        if emit:
            emit("tool_denied", {
                "call_id": call_id,
                "tool": tool_name,
                "reason": "Blocked by content filter",
                "shield_detail": result.detail,
            })
        return {"permissionDecision": "deny"}, shield_info

    logger.info(
        "[hitl] Prompt Shield passed: tool=%s call_id=%s elapsed=%.0fms",
        tool_name, call_id, elapsed_ms,
    )
    return None, shield_info
