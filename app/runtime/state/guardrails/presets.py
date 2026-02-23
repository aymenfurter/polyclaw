"""Preset definitions and background agent metadata for guardrails."""

from __future__ import annotations

from typing import Any

from .risk import _risk_of

# ── Background agent metadata ───────────────────────────────────────────

BACKGROUND_AGENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "scheduler",
        "name": "Scheduler",
        "description": (
            "Runs scheduled tasks on a cron schedule.  Has full tool access "
            "including file operations, terminal, and MCP servers."
        ),
        "has_tools": True,
        "default_policy": "background",
        "risk_note": (
            "Changing the policy for the scheduler may cause scheduled tasks "
            "to hang waiting for approval or fail silently."
        ),
    },
    {
        "id": "bot_processor",
        "name": "Bot Message Processor",
        "description": (
            "Processes messages from Teams, Telegram, and other bot channels.  "
            "Shares the full tool set with the interactive agent."
        ),
        "has_tools": True,
        "default_policy": "background",
        "risk_note": (
            "Changing the policy for the bot processor may cause channel "
            "messages to hang or tools to be blocked for bot users."
        ),
    },
    {
        "id": "proactive_loop",
        "name": "Proactive Loop",
        "description": (
            "Generates proactive messages and notifications.  Text-only -- "
            "has no tool access."
        ),
        "has_tools": False,
        "default_policy": "allow",
        "risk_note": (
            "This agent has no tool access. Guardrail changes have no effect."
        ),
    },
    {
        "id": "memory_formation",
        "name": "Memory Formation",
        "description": (
            "Post-processes conversations to extract and store memories.  "
            "Text-only -- has no tool access."
        ),
        "has_tools": False,
        "default_policy": "allow",
        "risk_note": (
            "This agent has no tool access. Guardrail changes have no effect."
        ),
    },
    {
        "id": "aitl_reviewer",
        "name": "AITL Reviewer",
        "description": (
            "AI reviewer that evaluates tool calls for safety.  Uses one "
            "internal decision tool (submit_decision)."
        ),
        "has_tools": True,
        "default_policy": "allow",
        "risk_note": (
            "The AITL reviewer IS the guardrail.  Restricting it will "
            "prevent it from functioning and break AITL-based approvals."
        ),
    },
    {
        "id": "realtime",
        "name": "Realtime Voice Agent",
        "description": (
            "Bridges the Realtime voice model to the Copilot SDK agent.  "
            "Spawns one-shot sessions to execute tool-based tasks requested "
            "via voice calls."
        ),
        "has_tools": True,
        "default_policy": "background",
        "risk_note": (
            "Changing the policy for the realtime agent may cause voice "
            "call tool invocations to hang or be blocked."
        ),
    },
)

_BACKGROUND_AGENT_IDS: frozenset[str] = frozenset(
    a["id"] for a in BACKGROUND_AGENTS
)


def list_background_agents() -> list[dict[str, Any]]:
    """Return metadata for all background agents."""
    return list(BACKGROUND_AGENTS)


# ── Preset constants ────────────────────────────────────────────────────

PRESET_RESTRICTIVE = "restrictive"
PRESET_BALANCED = "balanced"
PRESET_PERMISSIVE = "permissive"

_TIER_TO_PRESET: dict[int, str] = {
    1: PRESET_PERMISSIVE,
    2: PRESET_BALANCED,
    3: PRESET_RESTRICTIVE,
}

# Cross-reference: (selected_preset, model_tier) -> effective preset for model-column
# policies.  This ensures that switching presets actually changes per-model rules
# while still respecting each model's inherent safety tier.
_EFFECTIVE_MODEL_PRESET: dict[tuple[str, int], str] = {
    # Permissive preset: strong/standard models get permissive, cautious gets balanced
    (PRESET_PERMISSIVE, 1): PRESET_PERMISSIVE,
    (PRESET_PERMISSIVE, 2): PRESET_PERMISSIVE,
    (PRESET_PERMISSIVE, 3): PRESET_BALANCED,
    # Balanced preset: strong gets permissive, standard balanced, cautious balanced
    (PRESET_BALANCED, 1): PRESET_PERMISSIVE,
    (PRESET_BALANCED, 2): PRESET_BALANCED,
    (PRESET_BALANCED, 3): PRESET_BALANCED,
    # Restrictive preset: strong gets balanced, standard/cautious get restrictive
    (PRESET_RESTRICTIVE, 1): PRESET_BALANCED,
    (PRESET_RESTRICTIVE, 2): PRESET_RESTRICTIVE,
    (PRESET_RESTRICTIVE, 3): PRESET_RESTRICTIVE,
}

# Strategy lookup: (preset, context, risk) -> strategy
_PRESET_MATRIX: dict[str, dict[str, dict[str, str]]] = {
    PRESET_PERMISSIVE: {
        "interactive": {"low": "filter", "medium": "filter", "high": "filter"},
        "background":  {"low": "filter", "medium": "filter", "high": "hitl"},
    },
    PRESET_BALANCED: {
        "interactive": {"low": "filter", "medium": "filter", "high": "hitl"},
        "background":  {"low": "filter", "medium": "hitl",  "high": "deny"},
    },
    PRESET_RESTRICTIVE: {
        "interactive": {"low": "filter", "medium": "hitl", "high": "hitl"},
        "background":  {"low": "filter", "medium": "deny", "high": "deny"},
    },
}

# Per-preset tool overrides applied *after* the risk matrix.
_PRESET_OVERRIDES: dict[str, dict[str, dict[str, str]]] = {
    PRESET_BALANCED: {
        "background": {
            "create": "aitl",
            "edit": "aitl",
            "run": "aitl",
            "bash": "aitl",
            "make_voice_call": "aitl",
        },
    },
}

# Every tool/MCP/skill that presets should populate explicitly.
_ALL_PRESET_TOOL_IDS: list[str] = [
    # SDK
    "create", "edit", "view", "grep", "glob", "run", "bash",
    # Custom agent tools
    "schedule_task", "cancel_task", "list_scheduled_tasks", "make_voice_call",
    "send_adaptive_card", "send_hero_card", "send_thumbnail_card", "send_card_carousel",
    "search_memories_tool",
    # MCP
    "mcp:microsoft-learn", "mcp:playwright", "mcp:github-mcp-server", "mcp:azure-mcp-server",
    # Skills (builtin)
    "skill:web-search", "skill:summarize-url", "skill:note-taking", "skill:daily-briefing",
]

# Restrictiveness ranking for merging model policies across contexts.
_STRATEGY_RANK: dict[str, int] = {
    "allow": 0,
    "filter": 1,
    "aitl": 2,
    "hitl": 3,
    "pitl": 4,
    "ask": 4,
    "deny": 5,
}


def _strategy_rank(strategy: str) -> int:
    return _STRATEGY_RANK.get(strategy, 3)


def _build_preset_policies(preset: str) -> dict[str, Any]:
    """Return context_defaults and tool_policies for a given preset name.

    Uses the ``_PRESET_MATRIX`` to map (preset, context, risk) -> strategy
    for every known tool/MCP/skill.
    """
    matrix = _PRESET_MATRIX.get(preset, _PRESET_MATRIX[PRESET_RESTRICTIVE])
    overrides = _PRESET_OVERRIDES.get(preset, {})
    policies: dict[str, dict[str, str]] = {"interactive": {}, "background": {}}
    for tool_id in _ALL_PRESET_TOOL_IDS:
        risk = _risk_of(tool_id)
        for ctx in ("interactive", "background"):
            policies[ctx][tool_id] = matrix[ctx][risk]
    # Apply per-tool overrides after the matrix
    for ctx, tool_map in overrides.items():
        for tool_id, strategy in tool_map.items():
            policies[ctx][tool_id] = strategy
    # Context-level defaults (for tools not explicitly listed)
    ctx_defaults = {
        ctx: matrix[ctx]["medium"] for ctx in ("interactive", "background")
    }
    return {
        "context_defaults": ctx_defaults,
        "tool_policies": policies,
    }


def list_presets() -> list[dict[str, Any]]:
    """Return metadata for all available presets."""
    from .risk import _MODEL_TIERS

    return [
        {
            "id": PRESET_RESTRICTIVE,
            "name": "Restrictive",
            "description": (
                "For smaller or older models. Read-only tools allowed; "
                "file edits and browser require HITL in interactive; "
                "terminal, GitHub, Azure, and all MCP denied in background."
            ),
            "tier": 3,
            "recommended_for": sorted(
                m for m, t in _MODEL_TIERS.items() if t == 3
            ),
        },
        {
            "id": PRESET_BALANCED,
            "name": "Balanced",
            "description": (
                "For standard models. Low-risk tools allowed everywhere; "
                "terminal and GitHub/Azure require HITL in interactive; "
                "file operations, terminal, and voice calls use AITL in "
                "background; high-risk MCP denied in background. "
                "MS Learn allowed."
            ),
            "tier": 2,
            "recommended_for": sorted(
                m for m, t in _MODEL_TIERS.items() if t == 2
            ),
        },
        {
            "id": PRESET_PERMISSIVE,
            "name": "Permissive",
            "description": (
                "For strong frontier models. All tools allowed in interactive. "
                "Terminal, GitHub, Azure still require HITL in background. "
                "MS Learn, file operations, and browser allowed everywhere."
            ),
            "tier": 1,
            "recommended_for": sorted(
                m for m, t in _MODEL_TIERS.items() if t == 1
            ),
        },
    ]
