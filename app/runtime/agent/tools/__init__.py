"""Custom tools exposed to the Copilot agent."""

from .cards import CARD_TOOLS
from .memory import SearchMemoriesParams, search_memories_tool
from .scheduler import (
    CancelTaskParams,
    ScheduleTaskParams,
    cancel_task,
    list_scheduled_tasks,
    schedule_task,
)
from .voice import MakeCallParams, make_voice_call

ALL_TOOLS = [schedule_task, cancel_task, list_scheduled_tasks, make_voice_call] + CARD_TOOLS


def get_all_tools() -> list:
    from ...state.foundry_iq_config import get_foundry_iq_config

    tools = list(ALL_TOOLS)
    try:
        fiq = get_foundry_iq_config()
        if fiq.enabled and fiq.is_configured:
            tools.append(search_memories_tool)
    except Exception:
        pass
    return tools


__all__ = [
    "ALL_TOOLS",
    "CancelTaskParams",
    "MakeCallParams",
    "ScheduleTaskParams",
    "SearchMemoriesParams",
    "cancel_task",
    "get_all_tools",
    "list_scheduled_tasks",
    "make_voice_call",
    "schedule_task",
    "search_memories_tool",
]
