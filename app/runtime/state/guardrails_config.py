"""Guardrails configuration -- HITL approval rules for tools and MCP servers."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..agent.policy_bridge import (
    build_engine,
    config_to_yaml,
    make_eval_context,
    validate_yaml,
    yaml_to_config,
)
from ..config.settings import cfg

logger = logging.getLogger(__name__)

_instance: GuardrailsConfigStore | None = None


@dataclass
class GuardrailRule:
    """A single approval rule for a tool or MCP server."""

    id: str = ""
    name: str = ""
    pattern: str = ""
    scope: str = "tool"  # "tool" | "mcp"
    action: str = "allow"  # "allow" | "deny" | "ask"
    enabled: bool = True
    description: str = ""
    # Context-aware policy fields
    contexts: list[str] = field(default_factory=list)  # [] = all contexts
    models: list[str] = field(default_factory=list)  # [] = all models
    hitl_channel: str = "chat"  # "chat" | "phone"

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())[:8]


_VALID_STRATEGIES = frozenset({"allow", "deny", "hitl", "pitl", "aitl", "filter", "ask"})

# ── Background agent metadata ───────────────────────────────────────────
# Each background agent gets its own execution context so policy can be
# set per-agent.  ``resolve_action`` falls back from the agent-specific
# context to ``"background"`` when no override exists.

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

# Set of agent context IDs for fast lookup in resolve_action fallback.
_BACKGROUND_AGENT_IDS: frozenset[str] = frozenset(
    a["id"] for a in BACKGROUND_AGENTS
)


def list_background_agents() -> list[dict[str, Any]]:
    """Return metadata for all background agents."""
    return list(BACKGROUND_AGENTS)

# ── Model tiers ──────────────────────────────────────────────────────────
# Tier 1 (cautious): large frontier models -- most access, highest risk posture
# Tier 2 (standard): capable mid-range models
# Tier 3 (safe): smaller / older models -- least access, lowest risk posture

_MODEL_TIERS: dict[str, int] = {
    # Tier 1 -- cautious (most permissive, highest risk)
    "gpt-5.3-codex": 1,
    "claude-opus-4.6": 1,
    "claude-opus-4.6-fast": 1,
    # Tier 2 -- standard
    "claude-sonnet-4.6": 2,
    "gpt-5.2": 2,
    "gemini-3-pro-preview": 2,
    # Tier 3 -- safe (most restrictive, lowest risk)
    "gpt-5-mini": 3,
    "gpt-4.1": 3,
}

_DEFAULT_TIER = 3  # Unknown models get the most restrictive tier

# SDK tool categories used by presets
_FILE_TOOLS = frozenset({"create", "edit", "view", "grep", "glob"})
_TERMINAL_TOOLS = frozenset({"run", "bash"})

# ── MCP / tool risk classification ──────────────────────────────────────
# Risk levels: low (read-only / public), medium (browser / scheduling),
# high (code repos, infra, phone calls).

_MCP_RISK: dict[str, str] = {
    "mcp:microsoft-learn": "low",       # read-only public docs
    "mcp:playwright": "medium",         # browser automation, can navigate sites
    "mcp:github-mcp-server": "high",    # create repos, PRs, push code
    "mcp:azure-mcp-server": "high",     # create/delete Azure resources
}

_SKILL_RISK: dict[str, str] = {
    "skill:daily-briefing": "low",      # read-only from local memory
    "skill:wiki-search": "low",         # read-only, public API
    "skill:wiki-summary": "low",
    "skill:wiki-deep-dive": "low",
    "skill:gh-status-check": "low",     # read-only, public API
    "skill:gh-incidents": "low",
    "skill:gh-maintenance": "low",
    "skill:web-search": "medium",       # browser-based
    "skill:summarize-url": "medium",    # browser-based
    "skill:note-taking": "medium",      # filesystem writes
    "skill:daily-rollover": "medium",   # M365 reads + file writes
    "skill:end-day": "medium",
    "skill:weekly-review": "medium",
    "skill:monthly-review": "medium",
    "skill:setup-foundry": "high",      # provisions Azure infra
    "skill:foundry-agent-chat": "high", # creates cloud agents
    "skill:foundry-code-interpreter": "high",
    "skill:setup-workiq": "medium",
    "skill:setup-wikipedia": "low",
}

_CUSTOM_TOOL_RISK: dict[str, str] = {
    "schedule_task": "medium",
    "cancel_task": "medium",
    "list_scheduled_tasks": "low",
    "make_voice_call": "high",
    "search_memories_tool": "low",
    "send_adaptive_card": "low",
    "send_hero_card": "low",
    "send_thumbnail_card": "low",
    "send_card_carousel": "low",
}


def _risk_of(tool_id: str) -> str:
    """Return the risk level for any tool/MCP/skill id."""
    if tool_id in _MCP_RISK:
        return _MCP_RISK[tool_id]
    if tool_id in _SKILL_RISK:
        return _SKILL_RISK[tool_id]
    if tool_id in _CUSTOM_TOOL_RISK:
        return _CUSTOM_TOOL_RISK[tool_id]
    # SDK tools
    if tool_id in ("view", "grep", "glob"):
        return "low"
    if tool_id in ("create", "edit"):
        return "medium"
    if tool_id in ("run", "bash"):
        return "high"
    # Unknown MCP or skill -- default to high for safety
    if tool_id.startswith("mcp:") or tool_id.startswith("skill:"):
        return "high"
    return "medium"


# ── Preset definitions ──────────────────────────────────────────────────

PRESET_MINIMAL = "minimal"
PRESET_SUPERVISED = "supervised"
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
    # Balanced preset: strong gets permissive, standard balanced, cautious restrictive
    (PRESET_BALANCED, 1): PRESET_PERMISSIVE,
    (PRESET_BALANCED, 2): PRESET_BALANCED,
    (PRESET_BALANCED, 3): PRESET_RESTRICTIVE,
    # Restrictive preset: strong gets balanced, standard/cautious get restrictive
    (PRESET_RESTRICTIVE, 1): PRESET_BALANCED,
    (PRESET_RESTRICTIVE, 2): PRESET_RESTRICTIVE,
    (PRESET_RESTRICTIVE, 3): PRESET_RESTRICTIVE,
}

# Strategy lookup: (preset, context, risk) -> strategy
# Rows: risk low / medium / high
# Columns: interactive / background

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
# Format: {preset: {context: {tool_id: strategy}}}
_PRESET_OVERRIDES: dict[str, dict[str, dict[str, str]]] = {
    PRESET_BALANCED: {
        "background": {
            "create": "filter",
            "edit": "filter",
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


def get_model_tier(model: str) -> int:
    """Return the security tier for a model (1=cautious, 2=standard, 3=safe)."""
    return _MODEL_TIERS.get(model, _DEFAULT_TIER)


def get_preset_for_model(model: str) -> str:
    """Return the recommended preset name for a model."""
    return _TIER_TO_PRESET.get(get_model_tier(model), PRESET_RESTRICTIVE)


def list_model_tiers() -> list[dict[str, Any]]:
    """Return all known models with their tier and recommended preset."""
    result: list[dict[str, Any]] = []
    _TIER_LABELS = {1: "Strong", 2: "Standard", 3: "Cautious"}
    for model, tier in sorted(_MODEL_TIERS.items(), key=lambda x: (x[1], x[0])):
        result.append({
            "model": model,
            "tier": tier,
            "tier_label": _TIER_LABELS.get(tier, "Unknown"),
            "preset": get_preset_for_model(model),
        })
    return result


def list_presets() -> list[dict[str, Any]]:
    """Return metadata for all available presets."""
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
                "browser and schedules HITL in background; high-risk denied "
                "in background. MS Learn allowed."
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


# Restrictiveness ranking for merging model policies across contexts.
# Higher rank = more restrictive.
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


@dataclass
class GuardrailsConfig:
    """Top-level guardrails configuration."""

    hitl_enabled: bool = False
    default_action: str = "allow"  # "allow" | "deny" | "hitl" | "pitl" | "aitl" | "filter"
    default_channel: str = "chat"  # "chat" | "phone"
    phone_number: str = ""  # E.164 number for phone verification
    aitl_model: str = "gpt-4.1"  # Model used by the AITL reviewer agent
    aitl_spotlighting: bool = True  # Spotlight untrusted content in AITL prompts
    filter_mode: str = "prompt_shields"  # always "prompt_shields"
    content_safety_endpoint: str = ""  # Azure Content Safety endpoint URL
    content_safety_key: str = ""  # Azure Content Safety API key
    rules: list[GuardrailRule] = field(default_factory=list)
    # Policy matrix fields (frontend-driven)
    context_defaults: dict[str, str] = field(default_factory=dict)
    tool_policies: dict[str, dict[str, str]] = field(default_factory=dict)
    # Model-specific columns: user-defined model identifiers
    model_columns: list[str] = field(default_factory=list)
    # Model-scoped policies: model -> context -> tool -> strategy
    model_policies: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)


class GuardrailsConfigStore:
    """JSON-file-backed guardrails configuration.

    The store maintains both a JSON file (UI state, phone numbers, AITL
    config, etc.) and a YAML policy file consumed by the agent-policy-guard
    ``PolicyEngine``.  Every mutation regenerates the YAML and rebuilds
    the engine so that ``resolve_action()`` always reflects the latest
    configuration.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (cfg.data_dir / "guardrails.json")
        self._policy_path = self._path.with_name("policy.yaml")
        self._config = GuardrailsConfig()
        self._engine = build_engine(self._generate_yaml())
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def config(self) -> GuardrailsConfig:
        return self._config

    @property
    def hitl_enabled(self) -> bool:
        return self._config.hitl_enabled

    @property
    def default_action(self) -> str:
        return self._config.default_action

    @property
    def rules(self) -> list[GuardrailRule]:
        return list(self._config.rules)

    def set_hitl_enabled(self, enabled: bool) -> None:
        self._config.hitl_enabled = enabled
        self._save()

    def set_default_action(self, action: str) -> None:
        if action not in _VALID_STRATEGIES:
            raise ValueError("action must be one of: %s" % ", ".join(sorted(_VALID_STRATEGIES)))
        self._config.default_action = action
        self._save()

    @property
    def default_channel(self) -> str:
        return self._config.default_channel

    @property
    def phone_number(self) -> str:
        return self._config.phone_number

    def set_default_channel(self, channel: str) -> None:
        if channel not in ("chat", "phone"):
            raise ValueError("channel must be 'chat' or 'phone'")
        self._config.default_channel = channel
        self._save()

    def set_phone_number(self, number: str) -> None:
        self._config.phone_number = number
        self._save()

    def set_aitl_model(self, model: str) -> None:
        self._config.aitl_model = model
        self._save()

    def set_aitl_spotlighting(self, enabled: bool) -> None:
        self._config.aitl_spotlighting = enabled
        self._save()

    def set_filter_mode(self, mode: str) -> None:
        if mode != "prompt_shields":
            raise ValueError("filter_mode must be 'prompt_shields'")
        self._config.filter_mode = mode
        self._save()

    def set_content_safety_endpoint(self, endpoint: str) -> None:
        self._config.content_safety_endpoint = endpoint
        self._save()

    def set_content_safety_key(self, key: str) -> None:
        self._config.content_safety_key = key
        self._save()

    def set_context_default(self, context: str, strategy: str) -> None:
        if strategy not in _VALID_STRATEGIES:
            raise ValueError("strategy must be one of: %s" % ", ".join(sorted(_VALID_STRATEGIES)))
        self._config.context_defaults[context] = strategy
        self._save()

    def remove_context_default(self, context: str) -> bool:
        """Remove a context-level default, reverting to fallback resolution."""
        if context in self._config.context_defaults:
            del self._config.context_defaults[context]
            self._save()
            return True
        return False

    def set_tool_policy(
        self, context: str, tool_id: str, strategy: str,
    ) -> None:
        if strategy not in _VALID_STRATEGIES:
            raise ValueError("strategy must be one of: %s" % ", ".join(sorted(_VALID_STRATEGIES)))
        if context not in self._config.tool_policies:
            self._config.tool_policies[context] = {}
        self._config.tool_policies[context][tool_id] = strategy
        self._save()

    def remove_tool_policy(self, context: str, tool_id: str) -> bool:
        policies = self._config.tool_policies.get(context, {})
        if tool_id in policies:
            del policies[tool_id]
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # Model columns
    # ------------------------------------------------------------------

    def add_model_column(self, model: str) -> None:
        if model not in self._config.model_columns:
            self._config.model_columns.append(model)
            self._save()

    def remove_model_column(self, model: str) -> bool:
        if model in self._config.model_columns:
            self._config.model_columns.remove(model)
            self._config.model_policies.pop(model, None)
            self._save()
            return True
        return False

    def set_model_policy(
        self, model: str, tool_id: str, strategy: str, context: str = "interactive",
    ) -> None:
        if strategy not in _VALID_STRATEGIES:
            raise ValueError("strategy must be one of: %s" % ", ".join(sorted(_VALID_STRATEGIES)))
        if model not in self._config.model_policies:
            self._config.model_policies[model] = {}
        if context not in self._config.model_policies[model]:
            self._config.model_policies[model][context] = {}
        self._config.model_policies[model][context][tool_id] = strategy
        self._save()

    def remove_model_policy(
        self, model: str, tool_id: str, context: str = "interactive",
    ) -> bool:
        ctx_policies = self._config.model_policies.get(model, {}).get(context, {})
        if tool_id in ctx_policies:
            del ctx_policies[tool_id]
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def apply_preset(self, preset: str, *, auto_models: bool = True) -> None:
        """Apply a named preset to context_defaults and tool_policies.

        This overwrites the existing context_defaults and tool_policies.
        When *auto_models* is ``True`` (default), the preset's recommended
        models are added as model columns with tier-appropriate policies.
        All existing model columns are also refreshed to reflect the new
        preset's risk posture.
        """
        valid = {PRESET_RESTRICTIVE, PRESET_BALANCED, PRESET_PERMISSIVE}
        if preset not in valid:
            raise ValueError("preset must be one of: %s" % ", ".join(sorted(valid)))
        policies = _build_preset_policies(preset)
        self._config.context_defaults = policies["context_defaults"]
        self._config.tool_policies = policies["tool_policies"]
        self._config.hitl_enabled = True
        if auto_models:
            # Add recommended models for this preset tier as model columns
            preset_meta = next((p for p in list_presets() if p["id"] == preset), None)
            if preset_meta:
                new_models = [
                    m for m in preset_meta["recommended_for"]
                    if m not in self._config.model_columns
                ]
                if new_models:
                    self.apply_model_defaults(new_models, preset=preset)
            # Refresh ALL existing model columns with the new preset's posture
            if self._config.model_columns:
                self.apply_model_defaults(preset=preset)
        self._save()

    def set_all_strategies(self, strategy: str) -> None:
        """Set every tool policy and context default to *strategy*.

        This is a bulk operation: all tools in ``_ALL_PRESET_TOOL_IDS``
        across interactive and background contexts are set to the given
        strategy, and both context defaults are also set.  All known
        models from ``_MODEL_TIERS`` are added as model columns with
        the same strategy applied to every tool across both contexts.
        Guardrails are enabled.
        """
        if strategy not in _VALID_STRATEGIES:
            raise ValueError(
                "strategy must be one of: %s" % ", ".join(sorted(_VALID_STRATEGIES))
            )
        policies: dict[str, dict[str, str]] = {"interactive": {}, "background": {}}
        for tool_id in _ALL_PRESET_TOOL_IDS:
            for ctx in ("interactive", "background"):
                policies[ctx][tool_id] = strategy
        self._config.context_defaults = {
            "interactive": strategy,
            "background": strategy,
        }
        self._config.tool_policies = policies
        # Populate all known models with the same strategy
        self._config.model_columns = sorted(_MODEL_TIERS.keys())
        model_policies: dict[str, dict[str, dict[str, str]]] = {}
        for model in self._config.model_columns:
            per_ctx: dict[str, dict[str, str]] = {}
            for ctx in ("interactive", "background"):
                per_ctx[ctx] = {tool_id: strategy for tool_id in _ALL_PRESET_TOOL_IDS}
            model_policies[model] = per_ctx
        self._config.model_policies = model_policies
        self._config.hitl_enabled = True
        self._save()

    def apply_model_defaults(
        self,
        models: list[str] | None = None,
        *,
        preset: str | None = None,
    ) -> None:
        """Auto-populate model columns with tier-appropriate policies.

        For each model, determines the effective preset via the
        ``_EFFECTIVE_MODEL_PRESET`` cross-reference of *preset* (the
        user-selected risk posture) and the model's inherent tier.
        Each model gets separate per-context policies (interactive
        and background).

        If *models* is ``None``, uses the existing ``model_columns``.
        If *preset* is ``None``, falls back to the model's own tier
        preset.
        """
        target_models = models if models is not None else list(self._config.model_columns)
        for model in target_models:
            if model not in self._config.model_columns:
                self._config.model_columns.append(model)
            tier = get_model_tier(model)
            if preset:
                effective = _EFFECTIVE_MODEL_PRESET.get(
                    (preset, tier),
                    get_preset_for_model(model),
                )
            else:
                effective = get_preset_for_model(model)
            matrix = _PRESET_MATRIX.get(effective, _PRESET_MATRIX[PRESET_RESTRICTIVE])
            overrides = _PRESET_OVERRIDES.get(effective, {})
            per_ctx: dict[str, dict[str, str]] = {}
            for ctx in ("interactive", "background"):
                ctx_overrides = overrides.get(ctx, {})
                ctx_policies: dict[str, str] = {}
                for tool_id in _ALL_PRESET_TOOL_IDS:
                    risk = _risk_of(tool_id)
                    ctx_policies[tool_id] = ctx_overrides.get(tool_id, matrix[ctx][risk])
                per_ctx[ctx] = ctx_policies
            self._config.model_policies[model] = per_ctx
        self._save()

    def add_rule(
        self,
        *,
        name: str,
        pattern: str,
        scope: str = "tool",
        action: str = "ask",
        enabled: bool = True,
        description: str = "",
        contexts: list[str] | None = None,
        models: list[str] | None = None,
        hitl_channel: str = "chat",
    ) -> GuardrailRule:
        if scope not in ("tool", "mcp"):
            raise ValueError("scope must be 'tool' or 'mcp'")
        if action not in _VALID_STRATEGIES:
            raise ValueError("action must be one of: %s" % ", ".join(sorted(_VALID_STRATEGIES)))
        if hitl_channel not in ("chat", "phone"):
            raise ValueError("hitl_channel must be 'chat' or 'phone'")
        rule = GuardrailRule(
            name=name,
            pattern=pattern,
            scope=scope,
            action=action,
            enabled=enabled,
            description=description,
            contexts=contexts or [],
            models=models or [],
            hitl_channel=hitl_channel,
        )
        self._config.rules.append(rule)
        self._save()
        return rule

    def update_rule(self, rule_id: str, **kwargs: Any) -> GuardrailRule | None:
        for rule in self._config.rules:
            if rule.id == rule_id:
                for k, v in kwargs.items():
                    if k == "id":
                        continue
                    if hasattr(rule, k):
                        setattr(rule, k, v)
                self._save()
                return rule
        return None

    def remove_rule(self, rule_id: str) -> bool:
        before = len(self._config.rules)
        self._config.rules = [r for r in self._config.rules if r.id != rule_id]
        if len(self._config.rules) < before:
            self._save()
            return True
        return False

    def get_rule(self, rule_id: str) -> GuardrailRule | None:
        for rule in self._config.rules:
            if rule.id == rule_id:
                return rule
        return None

    def resolve_action(
        self,
        tool_name: str,
        mcp_server: str | None = None,
        execution_context: str = "",
        model: str = "",
    ) -> str:
        """Determine the strategy for a given tool invocation.

        Delegates to the agent-policy-guard ``PolicyEngine`` which evaluates
        the generated YAML policy set.  The YAML encodes all context defaults,
        tool policies, model policies, legacy rules, and background-agent
        fallbacks.

        When ``hitl_enabled`` is ``False`` the engine already has ``allow``
        as its default effect and no policies are generated, so it returns
        ``"allow"`` for every call.
        """
        ctx = make_eval_context(
            tool_name=tool_name,
            mcp_server=mcp_server,
            execution_context=execution_context,
            model=model,
        )
        result = self._engine.resolve(ctx)
        logger.debug(
            "[guardrails.resolve] engine result: tool=%s ctx=%s model=%s -> %s",
            tool_name, execution_context, model, result,
        )
        return result

    def resolve_channel(
        self,
        tool_name: str,
        mcp_server: str | None = None,
        execution_context: str = "",
        model: str = "",
    ) -> str:
        """Determine the HITL channel for a tool invocation.

        Returns the ``hitl_channel`` of the first matching rule, or the
        store-level ``default_channel``.
        """
        if not self._config.hitl_enabled:
            return "chat"

        for rule in self._config.rules:
            if not rule.enabled:
                continue
            if rule.contexts and execution_context and execution_context not in rule.contexts:
                continue
            if rule.models and model:
                if not any(self._matches(m, model) for m in rule.models):
                    continue
            if rule.scope == "tool" and self._matches(rule.pattern, tool_name):
                return rule.hitl_channel
            if rule.scope == "mcp" and mcp_server and self._matches(rule.pattern, mcp_server):
                return rule.hitl_channel

        return self._config.default_channel

    def to_dict(self) -> dict[str, Any]:
        return {
            # Frontend-canonical fields
            "enabled": self._config.hitl_enabled,
            "default_strategy": self._config.default_action,
            "hitl_channel": self._config.default_channel,
            "context_defaults": dict(self._config.context_defaults),
            "tool_policies": {
                ctx: dict(policies)
                for ctx, policies in self._config.tool_policies.items()
            },
            "model_columns": list(self._config.model_columns),
            "model_policies": {
                model: {
                    ctx: dict(tool_map)
                    for ctx, tool_map in ctx_policies.items()
                }
                for model, ctx_policies in self._config.model_policies.items()
            },
            # Backend / legacy fields
            "hitl_enabled": self._config.hitl_enabled,
            "default_action": self._config.default_action,
            "default_channel": self._config.default_channel,
            "phone_number": self._config.phone_number,
            "aitl_model": self._config.aitl_model,
            "aitl_spotlighting": self._config.aitl_spotlighting,
            "filter_mode": self._config.filter_mode,
            "content_safety_endpoint": self._config.content_safety_endpoint,
            "rules": [asdict(r) for r in self._config.rules],
        }

    @staticmethod
    def _matches(pattern: str, name: str) -> bool:
        """Simple glob-style matching: '*' matches everything, prefix* matches prefix."""
        if pattern == "*":
            return True
        if pattern.endswith("*"):
            return name.startswith(pattern[:-1])
        return pattern == name

    def _load(self) -> None:
        if not self._path.exists():
            self._rebuild_engine()
            return
        try:
            raw = json.loads(self._path.read_text())
            self._config = GuardrailsConfig(
                hitl_enabled=raw.get("enabled", raw.get("hitl_enabled", False)),
                default_action=raw.get("default_strategy", raw.get("default_action", "allow")),
                default_channel=raw.get("hitl_channel", raw.get("default_channel", "chat")),
                phone_number=raw.get("phone_number", ""),
                aitl_model=raw.get("aitl_model", "gpt-4.1"),
                aitl_spotlighting=raw.get("aitl_spotlighting", True),
                filter_mode=raw.get("filter_mode", "prompt_shields"),
                content_safety_endpoint=raw.get("content_safety_endpoint", ""),
                content_safety_key=raw.get("content_safety_key", ""),
                rules=[
                    GuardrailRule(**{
                        k: v for k, v in r.items()
                        if k in GuardrailRule.__dataclass_fields__
                    })
                    for r in raw.get("rules", [])
                ],
                context_defaults=raw.get("context_defaults", {}),
                tool_policies=raw.get("tool_policies", {}),
                model_columns=raw.get("model_columns", []),
                model_policies=raw.get("model_policies", {}),
            )
            self._rebuild_engine()
        except Exception as exc:
            logger.warning("Failed to load guardrails config from %s: %s", self._path, exc)

    # ------------------------------------------------------------------
    # Policy YAML management
    # ------------------------------------------------------------------

    @property
    def policy_path(self) -> Path:
        """Path to the generated policy YAML file."""
        return self._policy_path

    def get_policy_yaml(self) -> str:
        """Return the current policy as a YAML string."""
        return self._generate_yaml()

    def set_policy_yaml(self, yaml_text: str) -> str | None:
        """Apply a raw YAML policy, updating the config to match.

        Returns ``None`` on success or an error message string.
        """
        error = validate_yaml(yaml_text)
        if error:
            return error
        try:
            parsed = yaml_to_config(yaml_text)
            self._config.default_action = parsed["default_action"]
            self._config.default_channel = parsed["default_channel"]
            self._config.context_defaults = parsed["context_defaults"]
            self._config.tool_policies = parsed["tool_policies"]
            self._config.model_columns = parsed["model_columns"]
            self._config.model_policies = parsed["model_policies"]
            if parsed.get("rules"):
                self._config.rules = [
                    GuardrailRule(**{
                        k: v for k, v in r.items()
                        if k in GuardrailRule.__dataclass_fields__
                    })
                    for r in parsed["rules"]
                ]
            self._save()
            return None
        except Exception as exc:
            logger.warning("[guardrails] failed to apply YAML: %s", exc, exc_info=True)
            return str(exc)

    def _generate_yaml(self) -> str:
        """Generate a policy YAML string from the current config."""
        return config_to_yaml(
            hitl_enabled=self._config.hitl_enabled,
            default_action=self._config.default_action,
            default_channel=self._config.default_channel,
            context_defaults=self._config.context_defaults,
            tool_policies=self._config.tool_policies,
            model_columns=self._config.model_columns,
            model_policies=self._config.model_policies,
            rules=[asdict(r) for r in self._config.rules],
        )

    def _rebuild_engine(self) -> None:
        """Rebuild the PolicyEngine from the current config."""
        yaml_text = self._generate_yaml()
        self._engine = build_engine(yaml_text)
        # Write the YAML file alongside the JSON for reference / expert mode
        try:
            self._policy_path.write_text(yaml_text)
        except Exception as exc:
            logger.warning("[guardrails] failed to write policy.yaml: %s", exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        self._rebuild_engine()


def get_guardrails_config(path: Path | None = None) -> GuardrailsConfigStore:
    """Module-level singleton accessor."""
    global _instance
    if _instance is None:
        _instance = GuardrailsConfigStore(path)
    return _instance


def _reset_guardrails_config() -> None:
    global _instance
    _instance = None


from ..util.singletons import register_singleton  # noqa: E402

register_singleton(_reset_guardrails_config)
