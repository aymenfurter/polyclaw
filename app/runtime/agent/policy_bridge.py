"""Bridge between GuardrailsConfig and the agent-policy-guard PolicyEngine.

Converts the runtime's guardrails configuration (nested dicts, presets,
model columns) into an agent-policy YAML document and evaluates tool
invocations through the guard ``PolicyEngine``.

The guard library is imported through a single top-level import so that
swapping to an externally-installed package later requires no code changes
here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

# ── Guard library imports ────────────────────────────────────────────────
# Published at https://github.com/agent-policy/guard (Python SDK under
# ``python/`` subdirectory).  Installed via the Git dependency in
# ``pyproject.toml``.
from agent_policy_guard import (
    EvalContext,
    PolicyEngine,
    PolicySet,
    load_policy_set_from_str,
)

logger = logging.getLogger(__name__)

# Priority bands for generated policies.  Lower number = higher priority.
# Cascade: model_policies > tool_policies > context_defaults > rules > global.
# Model policies are most specific (model + context + tool) so they win.
#
# Bands are spaced 10 000 apart so that even with thousands of policies per
# band (e.g. 8 models × 2 contexts × 40 tools = 640), counters never bleed
# into the next band.
_PRIORITY_MODEL_TOOL = 10_000   # model + context + tool  (most specific, wins)
_PRIORITY_CTX_TOOL = 20_000     # context + tool
_PRIORITY_CTX_DEFAULT = 30_000  # context catch-all (beats rules)
_PRIORITY_RULE = 80_000         # legacy rules (lowest explicit policy)

# Background agent IDs that fall back to "background" context.
_BG_AGENT_IDS = frozenset({
    "scheduler",
    "bot_processor",
    "proactive_loop",
    "memory_formation",
    "aitl_reviewer",
    "realtime",
})


def config_to_yaml(
    *,
    hitl_enabled: bool,
    default_action: str,
    default_channel: str,
    context_defaults: dict[str, str],
    tool_policies: dict[str, dict[str, str]],
    model_columns: list[str],
    model_policies: dict[str, dict[str, dict[str, str]]],
    rules: list[dict[str, Any]] | None = None,
) -> str:
    """Convert a guardrails config into an agent-policy YAML string.

    The generated document is a valid ``PolicySet`` that can be loaded by
    the guard library.  The conversion is deterministic: same input always
    produces the same YAML.

    When ``hitl_enabled`` is ``False`` no policies are emitted so the
    engine returns ``"allow"`` for every tool call.
    """
    # ── Short-circuit: guardrails disabled → everything allowed ──
    if not hitl_enabled:
        doc: dict[str, Any] = {
            "apiVersion": "agent-policy/v1",
            "kind": "PolicySet",
            "metadata": {
                "name": "polyclaw-guardrails",
                "description": "Guardrails disabled -- all tools allowed.",
            },
            "defaults": {"effect": "allow", "channel": default_channel},
            "policies": [],
        }
        return yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True)

    policies: list[dict[str, Any]] = []
    priority_counter = _PRIORITY_MODEL_TOOL

    # ── 1. Model-scoped tool policies (highest priority) ─────────
    for model in sorted(model_columns):
        ctx_map = model_policies.get(model, {})
        for ctx in sorted(ctx_map):
            tool_map = ctx_map[ctx]
            for tool in sorted(tool_map):
                effect = tool_map[tool]
                policies.append({
                    "id": f"model-{_safe_id(model)}-{ctx}-{_safe_id(tool)}",
                    "priority": priority_counter,
                    "condition": _build_condition(
                        modes=[ctx], tools=[tool], models=[model],
                    ),
                    "effect": effect,
                })
                priority_counter += 1

    # Reset counter for next band
    priority_counter = _PRIORITY_CTX_TOOL

    # ── 2. Context-scoped tool policies ──────────────────────────
    for ctx in sorted(tool_policies):
        tool_map = tool_policies[ctx]
        for tool in sorted(tool_map):
            effect = tool_map[tool]
            policies.append({
                "id": f"ctx-{ctx}-{_safe_id(tool)}",
                "priority": priority_counter,
                "condition": _build_condition(modes=[ctx], tools=[tool]),
                "effect": effect,
            })
            priority_counter += 1

    # ── 3. Legacy rules ──────────────────────────────────────────
    if rules:
        priority_counter = max(priority_counter, _PRIORITY_RULE)
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            cond: dict[str, Any] = {}
            pattern = rule.get("pattern", "")
            scope = rule.get("scope", "tool")
            if scope == "mcp":
                cond["mcp_servers"] = [pattern]
            else:
                cond["tools"] = [pattern]
            if rule.get("contexts"):
                cond["modes"] = rule["contexts"]
            if rule.get("models"):
                cond["models"] = rule["models"]
            policies.append({
                "id": f"rule-{rule.get('id', 'unknown')}",
                "name": rule.get("name", ""),
                "priority": priority_counter,
                "condition": cond,
                "effect": rule.get("action", "allow"),
            })
            if rule.get("hitl_channel") == "phone":
                policies[-1]["channel"] = "phone"
            priority_counter += 1

    # ── 4. Context-level defaults ────────────────────────────────
    priority_counter = _PRIORITY_CTX_DEFAULT
    for ctx in sorted(context_defaults):
        effect = context_defaults[ctx]
        policies.append({
            "id": f"ctx-default-{ctx}",
            "priority": priority_counter,
            "condition": {"modes": [ctx]},
            "effect": effect,
        })
        priority_counter += 1

    # ── Context fallbacks for background agents ──────────────────
    context_fallbacks: dict[str, str] = {}
    for agent_id in sorted(_BG_AGENT_IDS):
        if agent_id != "background":
            context_fallbacks[agent_id] = "background"

    # ── Determine effective default ──────────────────────────────
    effective_default = default_action if hitl_enabled else "allow"

    doc: dict[str, Any] = {
        "apiVersion": "agent-policy/v1",
        "kind": "PolicySet",
        "metadata": {
            "name": "polyclaw-guardrails",
            "description": "Auto-generated from the Polyclaw guardrails UI.",
        },
        "defaults": {
            "effect": effective_default,
            "channel": default_channel,
        },
        "context_fallbacks": context_fallbacks,
        "policies": policies,
    }
    return yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True)


def yaml_to_config(yaml_text: str) -> dict[str, Any]:
    """Parse an agent-policy YAML string back into guardrails config fields.

    Returns a dict with ``context_defaults``, ``tool_policies``,
    ``model_policies``, ``model_columns``, ``default_action``, and
    ``default_channel`` that can be applied to the GuardrailsConfig.
    """
    ps = load_policy_set_from_str(yaml_text)

    default_action = ps.defaults.effect.value
    default_channel = ps.defaults.channel.value

    context_defaults: dict[str, str] = {}
    tool_policies: dict[str, dict[str, str]] = {}
    model_columns_set: set[str] = set()
    model_policies: dict[str, dict[str, dict[str, str]]] = {}
    rules: list[dict[str, Any]] = []

    for policy in ps.policies:
        cond = policy.condition
        effect = policy.effect.value

        has_model = cond.models is not None and len(cond.models) > 0
        has_tool = cond.tools is not None and len(cond.tools) > 0
        has_mcp = cond.mcp_servers is not None and len(cond.mcp_servers) > 0
        has_mode = cond.modes is not None and len(cond.modes) > 0

        # Model-scoped tool policy
        if has_model and (has_tool or has_mcp) and has_mode:
            for model in cond.models:  # type: ignore[union-attr]
                model_columns_set.add(model)
                for mode in cond.modes:  # type: ignore[union-attr]
                    items = list(cond.tools or []) + [
                        f"mcp:{s}" for s in (cond.mcp_servers or [])
                    ]
                    for tool_id in items:
                        model_policies.setdefault(model, {}).setdefault(mode, {})[tool_id] = effect
            continue

        # Context-scoped tool or MCP policy (no model)
        if (has_tool or has_mcp) and has_mode and not has_model:
            for mode in cond.modes:  # type: ignore[union-attr]
                items = list(cond.tools or []) + [
                    f"mcp:{s}" for s in (cond.mcp_servers or [])
                ]
                for tool_id in items:
                    tool_policies.setdefault(mode, {})[tool_id] = effect
            continue

        # Context default (mode only, no tools/models)
        if has_mode and not has_tool and not has_mcp and not has_model:
            for mode in cond.modes:  # type: ignore[union-attr]
                context_defaults[mode] = effect
            continue

        # Legacy rule (has tools or mcp but no mode)
        if (has_tool or has_mcp) and not has_mode:
            rule: dict[str, Any] = {
                "id": policy.id,
                "name": policy.name,
                "enabled": policy.enabled,
                "action": effect,
                "hitl_channel": policy.channel.value,
            }
            if has_mcp:
                rule["scope"] = "mcp"
                rule["pattern"] = (cond.mcp_servers or [""])[0]
            else:
                rule["scope"] = "tool"
                rule["pattern"] = (cond.tools or [""])[0]
            if has_model:
                rule["models"] = list(cond.models)  # type: ignore[arg-type]
            rules.append(rule)

    return {
        "default_action": default_action,
        "default_channel": default_channel,
        "context_defaults": context_defaults,
        "tool_policies": tool_policies,
        "model_columns": sorted(model_columns_set),
        "model_policies": model_policies,
        "rules": rules,
    }


def build_engine(yaml_text: str) -> PolicyEngine:
    """Build a PolicyEngine from a YAML string."""
    ps = load_policy_set_from_str(yaml_text)
    return PolicyEngine(ps)


def make_eval_context(
    tool_name: str,
    mcp_server: str | None = None,
    execution_context: str = "",
    model: str = "",
) -> EvalContext:
    """Build an EvalContext from the runtime's call parameters."""
    return EvalContext(
        tool=tool_name,
        mode=execution_context or "interactive",
        model=model,
        mcp_server=mcp_server or "",
    )


def validate_yaml(yaml_text: str) -> str | None:
    """Validate an agent-policy YAML string.

    Returns ``None`` if valid, or an error message string.
    """
    try:
        load_policy_set_from_str(yaml_text)
        return None
    except Exception as exc:
        return str(exc)


# ── Helpers ──────────────────────────────────────────────────────────────

def _safe_id(value: str) -> str:
    """Make a string safe for use in a policy ID."""
    return value.replace(":", "-").replace("*", "x").replace(" ", "-").replace("/", "-")


def _build_condition(
    *,
    modes: list[str] | None = None,
    tools: list[str] | None = None,
    models: list[str] | None = None,
    mcp_servers: list[str] | None = None,
) -> dict[str, Any]:
    """Build a condition dict, splitting tool IDs that start with ``mcp:``."""
    cond: dict[str, Any] = {}
    if modes:
        cond["modes"] = modes
    if models:
        cond["models"] = models

    tool_list: list[str] = []
    mcp_list: list[str] = list(mcp_servers or [])

    for t in (tools or []):
        if t.startswith("mcp:"):
            mcp_list.append(t[4:])  # strip "mcp:" prefix for guard format
        else:
            tool_list.append(t)

    if tool_list:
        cond["tools"] = tool_list
    if mcp_list:
        cond["mcp_servers"] = mcp_list
    return cond
