"""Risk classification and model tier definitions for guardrails."""

from __future__ import annotations

from typing import Any

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


def get_model_tier(model: str) -> int:
    """Return the security tier for a model (1=cautious, 2=standard, 3=safe)."""
    return _MODEL_TIERS.get(model, _DEFAULT_TIER)


def get_preset_for_model(model: str) -> str:
    """Return the recommended preset name for a model."""
    from .presets import _TIER_TO_PRESET, PRESET_RESTRICTIVE

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
