"""Scheduler tools -- create, cancel, and list scheduled tasks."""

from __future__ import annotations

import logging

from copilot import define_tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ScheduleTaskParams(BaseModel):
    description: str = Field(description="Human-readable description of the task")
    prompt: str = Field(description="The prompt to send to the agent when this task fires")
    cron: str | None = Field(
        default=None,
        description=(
            "Cron expression for recurring tasks (minute hour day month weekday). "
            "Minimum interval is every 1 hour. "
            "Example: '0 9 * * *' for every day at 09:00 UTC."
        ),
    )
    run_at: str | None = Field(
        default=None,
        description="ISO datetime for one-shot tasks (e.g. '2026-02-07T14:00:00')",
    )


class CancelTaskParams(BaseModel):
    task_id: str = Field(description="ID of the scheduled task to cancel")


@define_tool(
    description=(
        "Schedule a future task. Provide either a cron expression for recurring "
        "tasks (minimum every 1 hour) or a run_at datetime for one-shot tasks."
    )
)
def schedule_task(params: ScheduleTaskParams) -> dict:
    from ...scheduler import get_scheduler

    scheduler = get_scheduler()
    logger.info(
        "[schedule_task] called: desc=%r, cron=%r, run_at=%r, prompt=%r",
        params.description, params.cron, params.run_at, params.prompt[:80] if params.prompt else None,
    )
    try:
        task = scheduler.add(
            description=params.description,
            prompt=params.prompt,
            cron=params.cron,
            run_at=params.run_at,
        )
        logger.info(
            "[schedule_task] created task id=%s, run_at=%s, cron=%s, notify_cb=%s",
            task.id, task.run_at, task.cron,
            "SET" if scheduler._notify else "NOT SET",
        )
        return {"id": task.id, "description": task.description, "status": "scheduled"}
    except ValueError as exc:
        logger.warning("[schedule_task] rejected: %s", exc)
        return {"error": str(exc)}


@define_tool(description="Cancel a scheduled task by ID.")
def cancel_task(params: CancelTaskParams) -> str:
    from ...scheduler import get_scheduler

    scheduler = get_scheduler()
    return (
        f"Task {params.task_id} cancelled."
        if scheduler.remove(params.task_id)
        else f"Task {params.task_id} not found."
    )


@define_tool(
    description="List all scheduled tasks with their ID, description, schedule, and status.",
)
def list_scheduled_tasks() -> list[dict]:
    from ...scheduler import get_scheduler

    return [
        {
            "id": t.id,
            "description": t.description,
            "cron": t.cron,
            "run_at": t.run_at,
            "enabled": t.enabled,
            "last_run": t.last_run,
        }
        for t in get_scheduler().list_tasks()
    ]
