"""Background memory formation -- consolidates chat logs on idle.

After a configurable idle period, accumulated chat turns are sent to a
one-shot Copilot session that updates persistent memory files (daily
logs and topic notes). Runs entirely in the background.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config.settings import cfg
from .proactive import get_proactive_store

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@dataclass
class _ChatEntry:
    role: str
    text: str
    timestamp: str


class MemoryFormation:
    """Tracks conversation turns and triggers background memory writes on idle."""

    def __init__(self) -> None:
        self._log: list[_ChatEntry] = []
        self._idle_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._forming = False
        self._formation_count: int = 0
        self._last_formed_at: str | None = None
        self._last_turns_processed: int = 0
        self._last_error: str | None = None
        self._last_proactive_scheduled: bool = False

    def record(self, role: str, text: str) -> None:
        if not text:
            return
        entry = _ChatEntry(
            role=role,
            text=text,
            timestamp=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        self._log.append(entry)
        self._restart_idle_timer()

    def get_status(self) -> dict[str, Any]:
        timer_active = bool(self._idle_task and not self._idle_task.done())
        return {
            "buffered_turns": len(self._log),
            "timer_active": timer_active,
            "forming_now": self._forming,
            "idle_minutes": cfg.memory_idle_minutes,
            "formation_count": self._formation_count,
            "last_formed_at": self._last_formed_at,
            "last_turns_processed": self._last_turns_processed,
            "last_error": self._last_error,
            "last_proactive_scheduled": self._last_proactive_scheduled,
        }

    # -- idle timer --------------------------------------------------------

    def _restart_idle_timer(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        try:
            self._idle_task = asyncio.create_task(self._idle_wait())
        except RuntimeError:
            logger.debug("No event loop -- idle timer deferred")

    async def _idle_wait(self) -> None:
        try:
            await asyncio.sleep(cfg.memory_idle_minutes * 60)
        except asyncio.CancelledError:
            return
        await self._form_memory()

    async def force_form(self) -> dict[str, Any]:
        """Trigger memory formation immediately, bypassing the idle timer."""
        if self._forming:
            return {"status": "already_running"}
        if not self._log:
            return {"status": "no_turns", "message": "No buffered turns to process."}
        # Cancel any pending idle timer since we're forming now
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        await self._form_memory()
        return {"status": "ok", "formation_count": self._formation_count}

    # -- memory formation --------------------------------------------------

    async def _form_memory(self) -> None:
        async with self._lock:
            if self._forming or not self._log:
                return
            self._forming = True
            entries = list(self._log)
            self._log.clear()

        try:
            from ..agent.one_shot import run_one_shot

            transcript = self._format_transcript(entries)
            prompt = self._build_prompt(transcript)
            system_message = self._build_system_message()

            logger.info("Memory formation: sending %d turns to %s", len(entries), cfg.memory_model)
            result = await run_one_shot(prompt, model=cfg.memory_model, system_message=system_message)
            if result:
                logger.info("Memory formation complete (%d chars returned).", len(result))
            else:
                logger.warning("Memory formation returned no result.")

            self._formation_count += 1
            self._last_formed_at = datetime.now(UTC).isoformat()
            self._last_turns_processed = len(entries)
            self._last_error = None
            self._last_proactive_scheduled = False

            if get_proactive_store().enabled:
                await self._process_proactive_followup()
                self._process_proactive_reaction()
        except Exception as exc:
            logger.error("Memory formation failed: %s", exc, exc_info=True)
            self._last_error = str(exc)
            async with self._lock:
                self._log = entries + self._log
        finally:
            async with self._lock:
                self._forming = False

    # -- prompt building ---------------------------------------------------

    @staticmethod
    def _format_transcript(entries: list[_ChatEntry]) -> str:
        lines: list[str] = []
        for e in entries:
            tag = "User" if e.role == "user" else "Assistant"
            lines.append(f"[{e.timestamp}] {tag}: {e.text}")
        return "\n\n".join(lines)

    @staticmethod
    def _build_system_message() -> str:
        from .profile import profile_path, _usage_path as _skill_usage_path

        template = (_TEMPLATES_DIR / "memory_prompt.md").read_text()

        proactive_section = ""
        if get_proactive_store().enabled:
            proactive_section = MemoryFormation._build_proactive_section()

        return template.format(
            memory_daily_dir=cfg.memory_daily_dir,
            memory_topics_dir=cfg.memory_topics_dir,
            profile_path=profile_path(),
            skill_usage_path=_skill_usage_path(),
            suggestions_path=cfg.data_dir / "suggestions.txt",
            data_dir=cfg.data_dir,
            memory_dir=cfg.memory_dir,
            proactive_section=proactive_section,
        )

    @staticmethod
    def _build_proactive_section() -> str:
        proactive_template = (_TEMPLATES_DIR / "proactive_prompt_section.md").read_text()
        store = get_proactive_store()

        session_timing = MemoryFormation._gather_session_timing()

        history = store.history[-5:]
        if history:
            history_lines = []
            for h in history:
                reaction_str = f" (reaction: {h.reaction})" if h.reaction else " (no reaction recorded)"
                history_lines.append(f'   - [{h.delivered_at[:16]}] "{h.message[:80]}"{reaction_str}')
            history_context = "\n".join(history_lines)
        else:
            history_context = "   No proactive messages sent yet."

        prefs = store.preferences
        pref_lines = [
            f"   - Minimum gap between proactive messages: {prefs.min_gap_hours} hours",
            f"   - Maximum proactive messages per day: {prefs.max_daily}",
        ]
        if prefs.avoided_topics:
            pref_lines.append(f"   - Topics to avoid: {', '.join(prefs.avoided_topics)}")
        if prefs.preferred_times:
            pref_lines.append(f"   - Preferred times: {prefs.preferred_times}")

        sent_today = store.messages_sent_today()
        hours_since = store.hours_since_last_sent()
        pref_lines.append(f"   - Messages sent in last 24h: {sent_today}")
        if hours_since is not None:
            pref_lines.append(f"   - Hours since last proactive message: {hours_since:.1f}")

        return proactive_template.format(
            proactive_path=cfg.data_dir / "proactive_followup.json",
            session_timing_context=session_timing,
            proactive_history_context=history_context,
            proactive_preferences="\n".join(pref_lines),
        )

    @staticmethod
    def _gather_session_timing() -> str:
        from .session_store import SessionStore

        session_store = SessionStore()
        sessions = session_store.list_sessions()

        if not sessions:
            return "   No previous sessions recorded."

        now = datetime.now(UTC)
        lines: list[str] = []

        recent = sessions[:10]
        lines.append(f"   Total sessions recorded: {len(sessions)}")
        lines.append(f"   Recent sessions (last {len(recent)}):")

        session_times: list[datetime] = []
        for s in recent:
            started = s.get("started_at", "")
            msgs = s.get("message_count", 0)
            channel = s.get("channel", "?")
            if started:
                lines.append(f"     - {started[:16]} ({msgs} msgs, {channel})")
                try:
                    dt = datetime.fromisoformat(started)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    session_times.append(dt)
                except ValueError:
                    pass

        if len(session_times) >= 2:
            gaps = [
                (session_times[i] - session_times[i + 1]).total_seconds() / 3600
                for i in range(len(session_times) - 1)
            ]
            avg_gap = sum(gaps) / len(gaps)
            lines.append(f"   Average gap between sessions: {avg_gap:.1f} hours")

        if session_times:
            hours_since_last = (now - session_times[0]).total_seconds() / 3600
            lines.append(f"   Hours since last session: {hours_since_last:.1f}")

        if len(session_times) >= 3:
            hours_of_day = [dt.hour for dt in session_times]
            lines.append(f"   User typically active between: {min(hours_of_day):02d}:00 - {max(hours_of_day):02d}:00 UTC")

        return "\n".join(lines)

    @staticmethod
    def _build_prompt(transcript: str) -> str:
        template = (_TEMPLATES_DIR / "memory_transcript_prompt.md").read_text()

        proactive_context = ""
        if get_proactive_store().enabled:
            store = get_proactive_store()
            if store.history:
                last = store.history[-1]
                reaction_path = cfg.data_dir / "proactive_reaction.json"
                proactive_context = (
                    "\n---PROACTIVE CONTEXT---\n"
                    "The last proactive follow-up sent to the user was:\n"
                    f'  Message: "{last.message}"\n'
                    f"  Delivered at: {last.delivered_at}\n"
                    f"  Reaction so far: {last.reaction or 'none recorded'}\n\n"
                    "If the user's first message in this transcript appears to be "
                    "a response to this proactive message, assess their reaction. "
                    "Write a JSON file to record the reaction:\n"
                    f"  Path: {reaction_path}\n"
                    '  Format: {{"reaction": "positive|negative|neutral", '
                    '"detail": "brief note"}}\n'
                    "If the reaction is negative, include what topic or approach "
                    "the user dislikes so it can be avoided in the future.\n"
                    "---END PROACTIVE CONTEXT---\n"
                )

        return template.format(
            transcript=transcript,
            proactive_context=proactive_context,
        )

    async def _process_proactive_followup(self) -> None:
        followup_path = cfg.data_dir / "proactive_followup.json"
        if not followup_path.exists():
            logger.info("No proactive_followup.json written by LLM -- no follow-up this cycle.")
            return

        try:
            raw = json.loads(followup_path.read_text())
            followup_path.unlink()

            message = raw.get("message", "").strip()
            deliver_at = raw.get("deliver_at", "").strip()
            context = raw.get("context", "").strip()

            if not message or not deliver_at:
                logger.debug("Proactive follow-up file incomplete, skipping.")
                return

            store = get_proactive_store()
            prefs = store.preferences
            if store.messages_sent_today() >= prefs.max_daily:
                logger.info("Proactive daily limit reached (%d), skipping.", prefs.max_daily)
                return

            hours_since = store.hours_since_last_sent()
            if hours_since is not None and hours_since < prefs.min_gap_hours:
                logger.info(
                    "Proactive gap too short (%.1fh < %dh), skipping.",
                    hours_since, prefs.min_gap_hours,
                )
                return

            store.schedule_followup(message=message, deliver_at=deliver_at, context=context)
            self._last_proactive_scheduled = True
            logger.info("Proactive follow-up scheduled: %s at %s", message[:50], deliver_at)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to process proactive follow-up: %s", exc, exc_info=True)
            try:
                followup_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _process_proactive_reaction() -> None:
        reaction_path = cfg.data_dir / "proactive_reaction.json"
        if not reaction_path.exists():
            return

        try:
            raw = json.loads(reaction_path.read_text())
            reaction_path.unlink()

            reaction = raw.get("reaction", "").strip().lower()
            detail = raw.get("detail", "").strip()

            if reaction not in ("positive", "negative", "neutral"):
                logger.debug("Invalid proactive reaction value: %r", reaction)
                return

            store = get_proactive_store()
            store.mark_latest_reaction(reaction, detail)
            logger.info("Proactive reaction recorded: %s (%s)", reaction, detail[:60])

            if reaction == "negative" and detail:
                prefs = store.preferences
                if detail.lower() not in [t.lower() for t in prefs.avoided_topics]:
                    store.update_preferences(avoided_topics=list(prefs.avoided_topics) + [detail])
                    logger.info("Added avoided topic from negative reaction: %s", detail)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to process proactive reaction: %s", exc, exc_info=True)
            try:
                reaction_path.unlink(missing_ok=True)
            except OSError:
                pass


# -- singleton -------------------------------------------------------------

_memory: MemoryFormation | None = None


def get_memory() -> MemoryFormation:
    global _memory
    if _memory is None:
        _memory = MemoryFormation()
    return _memory


def _reset_memory() -> None:
    global _memory
    _memory = None


from ..util.singletons import register_singleton
register_singleton(_reset_memory)
