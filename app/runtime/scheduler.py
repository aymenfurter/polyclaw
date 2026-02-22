"""Scheduler -- persistent task scheduling that spawns Copilot SDK sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from croniter import croniter

from .agent import one_shot as one_shot_mod
from .config.settings import cfg
from .util.singletons import register_singleton

logger = logging.getLogger(__name__)

SCHEDULED_MODEL = "gpt-4.1"
MIN_INTERVAL_SECONDS = 3600

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class ScheduledTask:
    id: str
    description: str
    prompt: str
    cron: str | None = None
    run_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_run: str | None = None
    enabled: bool = True


def _validate_cron(cron: str) -> None:
    if not croniter.is_valid(cron):
        raise ValueError(f"Invalid cron expression: {cron}")
    ref = datetime(2025, 1, 1, tzinfo=UTC)
    it = croniter(cron, ref)
    first = it.get_next(datetime)
    second = it.get_next(datetime)
    gap = (second - first).total_seconds()
    if gap < MIN_INTERVAL_SECONDS:
        raise ValueError(
            f"Cron fires every {int(gap)}s -- minimum allowed interval is "
            f"{MIN_INTERVAL_SECONDS}s (1 hour)."
        )


def _cron_matches(expr: str, dt: datetime) -> bool:
    if not croniter.is_valid(expr):
        return False
    return croniter.match(expr, dt)


class _TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: dict[str, ScheduledTask] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            for entry in raw:
                self.items[entry["id"]] = ScheduledTask(**entry)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load scheduler DB: %s", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(t) for t in self.items.values()], indent=2))


class Scheduler:
    def __init__(self, path: Path | None = None) -> None:
        self._store = _TaskStore(path or cfg.scheduler_db_path)
        self._notify: Callable[[str], Awaitable[None]] | None = None
        self._hitl_interceptor: Any | None = None  # shared HitlInterceptor from Agent
        self._active_interceptor: Any | None = None  # per-task interceptor

    def set_notify_callback(self, cb: Callable[[str], Awaitable[None]]) -> None:
        self._notify = cb

    def set_hitl_interceptor(self, hitl: Any) -> None:
        """Bind the shared HitlInterceptor for access to AITL/filter/phone."""
        self._hitl_interceptor = hitl

    def add(
        self,
        description: str,
        prompt: str,
        cron: str | None = None,
        run_at: str | None = None,
    ) -> ScheduledTask:
        if not cron and not run_at:
            raise ValueError("Provide either cron or run_at")
        if cron:
            _validate_cron(cron)
        task = ScheduledTask(
            id=uuid.uuid4().hex[:8],
            description=description,
            prompt=prompt,
            cron=cron,
            run_at=run_at,
        )
        self._store.items[task.id] = task
        self._store.save()
        return task

    def remove(self, task_id: str) -> bool:
        if task_id in self._store.items:
            del self._store.items[task_id]
            self._store.save()
            return True
        return False

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._store.items.values())

    # ------------------------------------------------------------------
    # Background HITL approval bridge
    # ------------------------------------------------------------------

    @property
    def has_pending_approval(self) -> bool:
        """Whether a running scheduled task is waiting for HITL approval."""
        return (
            self._active_interceptor is not None
            and self._active_interceptor.has_pending_approval
        )

    def resolve_pending_approval(self, text: str) -> bool:
        """Resolve a pending scheduled-task approval with the user's reply.

        Returns ``True`` if a pending approval was found and resolved.
        """
        if self._active_interceptor is None:
            return False
        return self._active_interceptor.resolve_bot_reply(text)

    def get(self, task_id: str) -> ScheduledTask | None:
        return self._store.items.get(task_id)

    def update(self, task_id: str, **fields: Any) -> ScheduledTask | None:
        task = self._store.items.get(task_id)
        if not task:
            return None
        allowed = {"description", "prompt", "cron", "run_at", "enabled"}
        for key, val in fields.items():
            if key in allowed:
                if key == "cron" and val:
                    _validate_cron(val)
                setattr(task, key, val)
        self._store.save()
        return task

    def check_due(self) -> list[ScheduledTask]:
        now = datetime.now(UTC)
        due: list[ScheduledTask] = []
        logger.debug(
            "[scheduler] check_due tick at %s -- %d tasks in store",
            now.isoformat(), len(self._store.items),
        )

        for task in self._store.items.values():
            if not task.enabled:
                logger.debug(
                    "[scheduler]   task %s (%s) -- skipped (disabled)",
                    task.id, task.description,
                )
                continue

            if task.run_at:
                try:
                    run_dt = datetime.fromisoformat(task.run_at)
                    if run_dt.tzinfo is None:
                        run_dt = run_dt.replace(tzinfo=UTC)
                    delta = (run_dt - now).total_seconds()
                    if now >= run_dt:
                        logger.info(
                            "[scheduler]   task %s (%s) IS DUE (run_at=%s, now=%s, delta=%.1fs)",
                            task.id, task.description, task.run_at, now.isoformat(), delta,
                        )
                        due.append(task)
                        task.last_run = now.isoformat()
                        task.enabled = False
                    else:
                        logger.debug(
                            "[scheduler]   task %s (%s) -- not yet due (run_at=%s, fires in %.0fs)",
                            task.id, task.description, task.run_at, delta,
                        )
                except ValueError as exc:
                    logger.warning(
                        "[scheduler]   task %s (%s) -- bad run_at value %r: %s",
                        task.id, task.description, task.run_at, exc,
                    )
                    continue

            elif task.cron and _cron_matches(task.cron, now):
                if task.last_run:
                    try:
                        last_dt = datetime.fromisoformat(task.last_run)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=UTC)
                        gap = (now - last_dt).total_seconds()
                        if gap < MIN_INTERVAL_SECONDS:
                            logger.debug(
                                "[scheduler]   task %s (%s) -- cron matches but too soon (%.0fs < %ds)",
                                task.id, task.description, gap, MIN_INTERVAL_SECONDS,
                            )
                            continue
                    except ValueError:
                        pass
                logger.info(
                    "[scheduler]   task %s (%s) IS DUE (cron=%s)",
                    task.id, task.description, task.cron,
                )
                due.append(task)
                task.last_run = now.isoformat()
            else:
                logger.debug(
                    "[scheduler]   task %s (%s) -- cron=%s, run_at=%s, no match",
                    task.id, task.description, task.cron, task.run_at,
                )

        logger.debug("[scheduler] check_due result: %d due tasks", len(due))
        if due:
            self._store.save()
        return due

    async def run_due_tasks(self) -> None:
        from .state.profile import log_interaction

        for task in self.check_due():
            logger.info(
                "[scheduler] FIRING task %s: %s (cron=%s, run_at=%s)",
                task.id, task.description, task.cron, task.run_at,
            )
            log_interaction("scheduled", channel="scheduler")
            try:
                logger.debug("[scheduler]   spawning one-shot session for task %s ...", task.id)
                result = await self._spawn_session(task)
                logger.debug(
                    "[scheduler]   one-shot result for task %s: %s",
                    task.id, (result or "(none)")[:200],
                )
                await self._send_notification(task, result)
            except Exception as exc:
                logger.error("[scheduler] task %s failed: %s", task.id, exc, exc_info=True)
            finally:
                self._active_interceptor = None

    async def _spawn_session(self, task: ScheduledTask) -> str | None:
        from .agent.tools import get_all_tools

        template = (_TEMPLATES_DIR / "scheduler_prompt.md").read_text()
        system_message = template.format(
            memory_daily_dir=cfg.memory_daily_dir,
            data_dir=cfg.data_dir,
        )
        return await one_shot_mod.run_one_shot(
            task.prompt,
            model=SCHEDULED_MODEL,
            system_message=system_message,
            tools=get_all_tools(),
            on_pre_tool_use=self._make_background_hook(SCHEDULED_MODEL),
        )

    def _make_background_hook(self, model: str) -> Callable[..., Any]:
        """Build a guardrails-aware pre-tool-use hook for background sessions.

        Creates a fresh ``HitlInterceptor`` with ``execution_context``
        set to ``"background"``.  The scheduler's notification callback is
        bound as ``bot_reply_fn`` so that HITL approval requests are sent
        to the user via proactive messaging.  The user's reply is routed
        back through ``resolve_pending_approval`` (called by ``bot.py``).

        PITL works if a ``PhoneVerifier`` is configured on the shared
        interceptor.  AITL and Prompt Shields are also forwarded.
        """
        from .agent.hitl import HitlInterceptor
        from .state.guardrails_config import get_guardrails_config

        store = get_guardrails_config()
        interceptor = HitlInterceptor(store)
        interceptor.set_execution_context("scheduler")
        interceptor.set_model(model)

        # Bind notification channel so HITL can interact with the user.
        if self._notify:
            interceptor.set_bot_reply_fn(self._notify)

        # Forward AITL / Prompt Shield / phone from the shared interceptor.
        if self._hitl_interceptor:
            if getattr(self._hitl_interceptor, "_aitl_reviewer", None):
                interceptor.set_aitl_reviewer(self._hitl_interceptor._aitl_reviewer)
            if getattr(self._hitl_interceptor, "_prompt_shield", None):
                interceptor.set_prompt_shield(self._hitl_interceptor._prompt_shield)
            if getattr(self._hitl_interceptor, "_phone_verifier", None):
                interceptor.set_phone_verifier(self._hitl_interceptor._phone_verifier)

        self._active_interceptor = interceptor
        return interceptor.on_pre_tool_use

    async def _send_notification(self, task: ScheduledTask, result: str | None) -> None:
        if not self._notify:
            logger.warning(
                "[scheduler] _send_notification: NO notify callback set -- "
                "task %s result will be lost!", task.id,
            )
            return
        msg = f"Scheduled task completed: **{task.description}**\n\n{result or '(no output)'}"
        logger.debug("[scheduler] sending notification for task %s via callback ...", task.id)
        try:
            await self._notify(msg)
            logger.info("[scheduler] notification sent for task %s", task.id)
        except Exception as exc:
            logger.error("[scheduler] notification send failed for task %s: %s", task.id, exc, exc_info=True)


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler


def set_scheduler(instance: Scheduler) -> None:
    global _scheduler
    _scheduler = instance


def _reset_scheduler() -> None:
    global _scheduler
    _scheduler = None


register_singleton(_reset_scheduler)


async def scheduler_loop(interval_seconds: int = 60) -> None:
    sched = get_scheduler()
    logger.info(
        "[scheduler] loop started (interval=%ds, notify_cb=%s)",
        interval_seconds, "SET" if sched._notify else "NOT SET",
    )
    while True:
        try:
            await sched.run_due_tasks()
        except Exception as exc:
            logger.error("[scheduler] loop error: %s", exc, exc_info=True)
        await asyncio.sleep(interval_seconds)
