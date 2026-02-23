"""Proactive message delivery -- background loop.

Two responsibilities:
1. **Deliver** pending messages that are due (scheduled by memory agent).
2. **Generate** new proactive messages autonomously when nothing is
   pending and enough idle time has passed, using memory context to
   craft something genuinely useful.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.settings import cfg
from ..state.proactive import get_proactive_store
from ..state.profile import log_interaction

if TYPE_CHECKING:
    from ..state.session_store import SessionStore

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Minimum hours since last user activity before we proactively reach out.
_MIN_USER_IDLE_HOURS = 1.0

# Cooldown between autonomous generation attempts (even if LLM says NO_FOLLOWUP).
_GENERATION_COOLDOWN_MINUTES = 60


def _in_preferred_window(prefs_times: str) -> bool:
    """Return True if current UTC hour falls within any preferred time range.

    *prefs_times* is a comma-separated string like ``"9:00-12:00, 14:00-17:00"``.
    If empty/blank, any time is allowed.
    """
    if not prefs_times or not prefs_times.strip():
        return True

    now_hour = datetime.now(UTC).hour
    for window in prefs_times.split(","):
        window = window.strip()
        if "-" not in window:
            continue
        try:
            start_s, end_s = window.split("-", 1)
            start_h = int(start_s.strip().split(":")[0])
            end_h = int(end_s.strip().split(":")[0])
            if start_h <= end_h:
                if start_h <= now_hour < end_h:
                    return True
            else:  # wraps midnight
                if now_hour >= start_h or now_hour < end_h:
                    return True
        except (ValueError, IndexError):
            continue
    return False


def _gather_memory_context() -> str:
    """Read the most recent daily log and a few topic files for LLM context."""
    lines: list[str] = []

    # Latest daily log
    daily_dir = cfg.memory_daily_dir
    if daily_dir.is_dir():
        logs = sorted(daily_dir.glob("*.md"), reverse=True)
        for log_path in logs[:2]:
            try:
                content = log_path.read_text()[:2000]
                lines.append(f"--- {log_path.name} ---\n{content}")
            except OSError:
                pass

    # A couple of topic notes
    topics_dir = cfg.memory_topics_dir
    if topics_dir.is_dir():
        topics = sorted(topics_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for t in topics[:3]:
            try:
                content = t.read_text()[:800]
                lines.append(f"--- topic: {t.stem} ---\n{content}")
            except OSError:
                pass

    return "\n\n".join(lines) if lines else "No memory files found yet."


def _gather_profile_context() -> str:
    """Read the user/agent profile JSON for LLM context."""
    from ..state.profile import profile_path

    path = profile_path()
    if path.exists():
        try:
            return path.read_text()[:1000]
        except OSError:
            pass
    return "No profile available."


def _hours_since_last_session() -> float | None:
    """Return hours since the most recent session's last update."""
    from ..state.session_store import SessionStore

    store = SessionStore()
    sessions = store.list_sessions()
    if not sessions:
        return None
    latest = sessions[0]
    ts = latest.get("updated_at") or latest.get("created_at")
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=UTC)
        elif isinstance(ts, str):
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        else:
            return None
        return (datetime.now(UTC) - dt).total_seconds() / 3600
    except (ValueError, OSError):
        return None


async def _generate_proactive_message() -> str | None:
    """Use a one-shot LLM call to generate a proactive message.

    Returns the message string or ``None`` if the LLM decided nothing
    is worth sending (``NO_FOLLOWUP``).
    """
    from ..agent.one_shot import run_one_shot

    template = (_TEMPLATES_DIR / "proactive_generate_prompt.md").read_text()
    store = get_proactive_store()

    # Build history context
    history = store.history[-5:]
    if history:
        history_lines = []
        for h in history:
            reaction = f" (reaction: {h.reaction})" if h.reaction else ""
            history_lines.append(f'- [{h.delivered_at[:16]}] "{h.message[:80]}"{reaction}')
        history_ctx = "\n".join(history_lines)
    else:
        history_ctx = "No proactive messages sent yet."

    hours_idle = _hours_since_last_session()

    prompt = template.format(
        memory_context=_gather_memory_context(),
        profile_context=_gather_profile_context(),
        proactive_history=history_ctx,
        current_time=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        hours_since_activity=f"{hours_idle:.1f}" if hours_idle is not None else "unknown",
    )

    logger.info("[proactive] generating autonomous message via LLM ...")
    try:
        result = await run_one_shot(
            prompt,
            model=cfg.memory_model,
            timeout=60,
        )
    except Exception as exc:
        logger.error("[proactive] generation LLM call failed: %s", exc)
        return None

    if not result:
        return None

    text = result.strip().strip('"').strip("'")
    if "NO_FOLLOWUP" in text.upper():
        logger.info("[proactive] LLM decided: NO_FOLLOWUP")
        return None

    # Sanity: reject very short or suspiciously long responses
    if len(text) < 10 or len(text) > 500:
        logger.warning("[proactive] LLM response rejected (len=%d): %s", len(text), text[:80])
        return None

    return text


def _should_auto_generate(store: "ProactiveStore") -> bool:  # noqa: F821
    """Decide whether the loop should autonomously generate a proactive message."""
    if not store.enabled:
        return False

    # Already have a pending message
    if store.pending:
        return False

    prefs = store.preferences

    # Daily limit
    if store.messages_sent_today() >= prefs.max_daily:
        logger.debug("[proactive] daily limit reached (%d/%d)", store.messages_sent_today(), prefs.max_daily)
        return False

    # Min gap since last sent
    hours_since = store.hours_since_last_sent()
    if hours_since is not None and hours_since < prefs.min_gap_hours:
        logger.debug("[proactive] too soon since last sent (%.1fh < %dh)", hours_since, prefs.min_gap_hours)
        return False

    # Preferred time window
    if not _in_preferred_window(prefs.preferred_times):
        logger.debug("[proactive] outside preferred time window")
        return False

    # User must have been idle for a minimum period
    user_idle = _hours_since_last_session()
    if user_idle is not None and user_idle < _MIN_USER_IDLE_HOURS:
        logger.debug("[proactive] user active too recently (%.1fh)", user_idle)
        return False

    return True


async def proactive_delivery_loop(
    notify: Callable[[str], Awaitable[bool]],
    interval_seconds: int = 60,
    session_store: "SessionStore | None" = None,
) -> None:
    """Check every *interval_seconds* for due proactive messages and deliver them.

    Also autonomously generates proactive messages when nothing is pending
    and the conditions are right (idle time, preferred window, limits).
    """
    store = get_proactive_store()
    logger.info("[proactive] delivery loop started (interval=%ds)", interval_seconds)

    last_generation_attempt: datetime | None = None

    while True:
        try:
            now = datetime.now(UTC)
            pending_obj = store.pending
            logger.debug(
                "[proactive] tick at %s -- enabled=%s, pending=%s, is_due=%s",
                now.isoformat(),
                store.enabled,
                f"id={pending_obj.id} deliver_at={pending_obj.deliver_at}" if pending_obj else "None",
                store.is_due() if pending_obj else "n/a",
            )

            # ── 1. Deliver pending messages that are due ──────────────
            if store.enabled and store.is_due():
                pending = store.clear_pending()
                if pending:
                    await _deliver_message(notify, store, pending, session_store)

            elif not store.enabled and pending_obj:
                logger.debug("[proactive] has pending message but proactive is DISABLED")

            # ── 2. Autonomous generation when nothing is pending ──────
            elif _should_auto_generate(store):
                cooldown_ok = (
                    last_generation_attempt is None
                    or (now - last_generation_attempt).total_seconds() > _GENERATION_COOLDOWN_MINUTES * 60
                )
                if cooldown_ok:
                    last_generation_attempt = now
                    message = await _generate_proactive_message()
                    if message:
                        # Schedule for immediate delivery
                        deliver_at = now.isoformat()
                        store.schedule_followup(
                            message=message,
                            deliver_at=deliver_at,
                            context="auto-generated",
                        )
                        logger.info("[proactive] auto-generated message scheduled for immediate delivery")
                        # Deliver right away
                        pending = store.clear_pending()
                        if pending:
                            await _deliver_message(notify, store, pending, session_store)

        except Exception as exc:
            logger.error("[proactive] delivery loop error: %s", exc, exc_info=True)

        await asyncio.sleep(interval_seconds)


async def _deliver_message(
    notify: Callable[[str], Awaitable[bool]],
    store: "ProactiveStore",  # noqa: F821
    pending: "PendingMessage",  # noqa: F821
    session_store: "SessionStore | None",
) -> None:
    """Attempt to deliver a pending proactive message."""
    from ..state.proactive import PendingMessage  # noqa: F811

    now = datetime.now(UTC)
    logger.info(
        "[proactive] DELIVERING message id=%s: %s (deliver_at=%s, now=%s)",
        pending.id, pending.message[:80], pending.deliver_at, now.isoformat(),
    )
    try:
        delivered = await notify(pending.message)
        logger.debug("[proactive] notify() returned: %s", delivered)
        if delivered:
            store.record_sent(
                message=pending.message,
                context=pending.context,
                created_at=pending.created_at,
                msg_id=pending.id,
            )
            if session_store is not None:
                session_store.record("system", f"[proactive] {pending.message}", channel="proactive")
            log_interaction("proactive", channel="proactive")
            logger.info("[proactive] message delivered successfully: %s", pending.id)
        else:
            logger.warning(
                "[proactive] message NOT delivered (no active channels): %s -- will retry in 5 min",
                pending.id,
            )
            retry_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
            store.schedule_followup(
                message=pending.message,
                deliver_at=retry_at,
                context=pending.context,
            )
    except Exception as exc:
        logger.error("[proactive] delivery failed: %s", exc, exc_info=True)
        retry_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        store.schedule_followup(
            message=pending.message,
            deliver_at=retry_at,
            context=pending.context,
        )
