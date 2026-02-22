"""Tests for the policy_bridge module -- config/YAML conversion and engine resolution."""

from __future__ import annotations

import pytest

from app.runtime.agent.policy_bridge import (
    build_engine,
    config_to_yaml,
    make_eval_context,
    validate_yaml,
    yaml_to_config,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _default_args(**overrides):
    """Return minimal config_to_yaml kwargs with overrides applied."""
    base = {
        "hitl_enabled": True,
        "default_action": "allow",
        "default_channel": "chat",
        "context_defaults": {},
        "tool_policies": {},
        "model_columns": [],
        "model_policies": {},
        "rules": None,
    }
    base.update(overrides)
    return base


def _resolve(yaml_text: str, **ctx_kwargs) -> str:
    """Build an engine from YAML and resolve a single context."""
    engine = build_engine(yaml_text)
    ctx = make_eval_context(**ctx_kwargs)
    return engine.resolve(ctx)


# ── 1. Disabled guardrails ──────────────────────────────────────────────

class TestDisabledGuardrails:
    """When hitl_enabled is False, everything is allowed."""

    def test_empty_config_returns_allow(self) -> None:
        yaml_text = config_to_yaml(**_default_args(hitl_enabled=False))
        assert _resolve(yaml_text, tool_name="run") == "allow"

    def test_disabled_ignores_tool_policies(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            hitl_enabled=False,
            tool_policies={"interactive": {"run": "deny"}},
        ))
        assert _resolve(yaml_text, tool_name="run", execution_context="interactive") == "allow"

    def test_disabled_ignores_model_policies(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            hitl_enabled=False,
            model_columns=["gpt-4.1"],
            model_policies={"gpt-4.1": {"interactive": {"run": "deny"}}},
        ))
        assert _resolve(
            yaml_text, tool_name="run",
            execution_context="interactive", model="gpt-4.1",
        ) == "allow"


# ── 2. Context tool policies ───────────────────────────────────────────

class TestContextToolPolicies:
    """Context-scoped tool policies (the basic policy matrix)."""

    def test_interactive_tool_policy(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            tool_policies={"interactive": {"run": "hitl", "view": "filter"}},
        ))
        assert _resolve(yaml_text, tool_name="run", execution_context="interactive") == "hitl"
        assert _resolve(yaml_text, tool_name="view", execution_context="interactive") == "filter"

    def test_background_tool_policy(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            tool_policies={"background": {"run": "deny", "view": "allow"}},
        ))
        assert _resolve(yaml_text, tool_name="run", execution_context="background") == "deny"
        assert _resolve(yaml_text, tool_name="view", execution_context="background") == "allow"

    def test_mcp_tool_policy(self) -> None:
        """Tool IDs starting with 'mcp:' should match via mcp_server field."""
        yaml_text = config_to_yaml(**_default_args(
            tool_policies={"interactive": {"mcp:github-mcp-server": "hitl"}},
        ))
        assert _resolve(
            yaml_text, tool_name="mcp:github-mcp-server",
            mcp_server="github-mcp-server",
            execution_context="interactive",
        ) == "hitl"

    def test_unknown_tool_falls_to_default(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            tool_policies={"interactive": {"run": "hitl"}},
        ))
        # "unknown_tool" is not in tool_policies -> falls to global default
        assert _resolve(
            yaml_text, tool_name="unknown_tool", execution_context="interactive",
        ) == "allow"


# ── 3. Context defaults ────────────────────────────────────────────────

class TestContextDefaults:
    """Context-level catch-all defaults."""

    def test_context_default_catches_unlisted_tools(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            context_defaults={"interactive": "hitl", "background": "deny"},
        ))
        assert _resolve(
            yaml_text, tool_name="any_tool", execution_context="interactive",
        ) == "hitl"
        assert _resolve(
            yaml_text, tool_name="any_tool", execution_context="background",
        ) == "deny"

    def test_tool_policy_beats_context_default(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            context_defaults={"interactive": "deny"},
            tool_policies={"interactive": {"run": "allow"}},
        ))
        # Tool policy wins
        assert _resolve(
            yaml_text, tool_name="run", execution_context="interactive",
        ) == "allow"
        # Other tools fall to context default
        assert _resolve(
            yaml_text, tool_name="bash", execution_context="interactive",
        ) == "deny"


# ── 4. Model-scoped policies ───────────────────────────────────────────

class TestModelPolicies:
    """Model-specific tool policies (fallback after context policies)."""

    def test_model_policy_resolves(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            model_columns=["gpt-4.1"],
            model_policies={"gpt-4.1": {"interactive": {"run": "deny"}}},
        ))
        assert _resolve(
            yaml_text, tool_name="run",
            execution_context="interactive", model="gpt-4.1",
        ) == "deny"

    def test_model_policy_beats_context_tool_policy(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            tool_policies={"interactive": {"run": "hitl"}},
            model_columns=["gpt-4.1"],
            model_policies={"gpt-4.1": {"interactive": {"run": "filter"}}},
        ))
        # Model policy (more specific) wins over context tool policy
        assert _resolve(
            yaml_text, tool_name="run",
            execution_context="interactive", model="gpt-4.1",
        ) == "filter"

    def test_model_policy_for_unlisted_tool(self) -> None:
        """Model policy applies when the tool is not in context tool_policies."""
        yaml_text = config_to_yaml(**_default_args(
            tool_policies={"interactive": {"view": "filter"}},
            model_columns=["gpt-4.1"],
            model_policies={"gpt-4.1": {"interactive": {"run": "deny"}}},
        ))
        # "run" is not in context tool policies, model policy applies
        assert _resolve(
            yaml_text, tool_name="run",
            execution_context="interactive", model="gpt-4.1",
        ) == "deny"
        # "view" is in context tool policies, that wins
        assert _resolve(
            yaml_text, tool_name="view",
            execution_context="interactive", model="gpt-4.1",
        ) == "filter"


# ── 5. Legacy rules ────────────────────────────────────────────────────

class TestLegacyRules:
    """Rules created via the rule CRUD API."""

    def test_rule_matches_tool(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            rules=[{
                "id": "r1",
                "name": "block-run",
                "pattern": "run",
                "scope": "tool",
                "action": "deny",
                "enabled": True,
            }],
        ))
        assert _resolve(yaml_text, tool_name="run") == "deny"

    def test_disabled_rule_ignored(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            rules=[{
                "id": "r1",
                "name": "block-run",
                "pattern": "run",
                "scope": "tool",
                "action": "deny",
                "enabled": False,
            }],
        ))
        assert _resolve(yaml_text, tool_name="run") == "allow"

    def test_mcp_scope_rule(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            rules=[{
                "id": "r2",
                "name": "block-gh",
                "pattern": "github-mcp-server",
                "scope": "mcp",
                "action": "deny",
                "enabled": True,
            }],
        ))
        assert _resolve(
            yaml_text, tool_name="some_tool", mcp_server="github-mcp-server",
        ) == "deny"

    def test_context_default_beats_rule(self) -> None:
        """Context defaults have higher priority than legacy rules."""
        yaml_text = config_to_yaml(**_default_args(
            context_defaults={"interactive": "allow"},
            rules=[{
                "id": "r3",
                "name": "block-custom",
                "pattern": "custom_tool",
                "scope": "tool",
                "action": "deny",
                "enabled": True,
            }],
        ))
        # Context default ("allow") wins over rule ("deny")
        assert _resolve(
            yaml_text, tool_name="custom_tool", execution_context="interactive",
        ) == "allow"
        # Without matching context default, rule fires
        assert _resolve(
            yaml_text, tool_name="custom_tool", execution_context="background",
        ) == "deny"


# ── 6. Priority cascade ────────────────────────────────────────────────

class TestPriorityCascade:
    """Validate: model > tool_policies > context_default > rules > global."""

    def test_full_cascade(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            default_action="allow",
            context_defaults={"interactive": "aitl"},
            tool_policies={"interactive": {"run": "hitl"}},
            model_columns=["gpt-4.1"],
            model_policies={"gpt-4.1": {"interactive": {"run": "filter"}}},
            rules=[{
                "id": "r1", "name": "test", "pattern": "run",
                "scope": "tool", "action": "deny", "enabled": True,
            }],
        ))
        # 1. Model policy wins (most specific) for "run" + interactive + gpt-4.1
        assert _resolve(
            yaml_text, tool_name="run",
            execution_context="interactive", model="gpt-4.1",
        ) == "filter"

    def test_model_as_fallback(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            default_action="allow",
            model_columns=["gpt-4.1"],
            model_policies={"gpt-4.1": {"interactive": {"bash": "deny"}}},
        ))
        # No context tool policy for bash, model policy applies
        assert _resolve(
            yaml_text, tool_name="bash",
            execution_context="interactive", model="gpt-4.1",
        ) == "deny"

    def test_global_default_is_last_resort(self) -> None:
        yaml_text = config_to_yaml(**_default_args(default_action="deny"))
        assert _resolve(
            yaml_text, tool_name="anything", execution_context="interactive",
        ) == "deny"


# ── 7. Background agent context fallbacks ───────────────────────────────

class TestContextFallbacks:
    """Background agents fall back to 'background' context."""

    def test_scheduler_falls_to_background(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            context_defaults={"background": "hitl"},
        ))
        assert _resolve(
            yaml_text, tool_name="run", execution_context="scheduler",
        ) == "hitl"

    def test_bot_processor_falls_to_background(self) -> None:
        yaml_text = config_to_yaml(**_default_args(
            context_defaults={"background": "deny"},
        ))
        assert _resolve(
            yaml_text, tool_name="run", execution_context="bot_processor",
        ) == "deny"


# ── 8. YAML round-trip ─────────────────────────────────────────────────

class TestYamlRoundTrip:
    """config_to_yaml -> yaml_to_config round-trip preserves semantics."""

    def test_basic_round_trip(self) -> None:
        original = _default_args(
            context_defaults={"interactive": "filter", "background": "hitl"},
            tool_policies={
                "interactive": {"run": "hitl", "view": "filter"},
                "background": {"run": "deny"},
            },
        )
        yaml_text = config_to_yaml(**original)
        parsed = yaml_to_config(yaml_text)
        assert parsed["default_action"] == "allow"
        assert parsed["default_channel"] == "chat"
        assert parsed["context_defaults"]["interactive"] == "filter"
        assert parsed["context_defaults"]["background"] == "hitl"
        assert parsed["tool_policies"]["interactive"]["run"] == "hitl"
        assert parsed["tool_policies"]["background"]["run"] == "deny"

    def test_model_round_trip(self) -> None:
        original = _default_args(
            model_columns=["gpt-4.1", "gpt-5-mini"],
            model_policies={
                "gpt-4.1": {"interactive": {"run": "deny"}},
                "gpt-5-mini": {"background": {"bash": "hitl"}},
            },
        )
        yaml_text = config_to_yaml(**original)
        parsed = yaml_to_config(yaml_text)
        assert "gpt-4.1" in parsed["model_columns"]
        assert "gpt-5-mini" in parsed["model_columns"]
        assert parsed["model_policies"]["gpt-4.1"]["interactive"]["run"] == "deny"
        assert parsed["model_policies"]["gpt-5-mini"]["background"]["bash"] == "hitl"

    def test_mcp_round_trip(self) -> None:
        original = _default_args(
            tool_policies={"interactive": {"mcp:github-mcp-server": "hitl"}},
        )
        yaml_text = config_to_yaml(**original)
        parsed = yaml_to_config(yaml_text)
        assert parsed["tool_policies"]["interactive"]["mcp:github-mcp-server"] == "hitl"


# ── 9. YAML validation ─────────────────────────────────────────────────

class TestYamlValidation:
    """validate_yaml() accepts valid and rejects invalid YAML."""

    def test_valid_yaml_returns_none(self) -> None:
        yaml_text = config_to_yaml(**_default_args())
        assert validate_yaml(yaml_text) is None

    def test_invalid_yaml_returns_error(self) -> None:
        error = validate_yaml("this is not valid yaml: [[[")
        assert error is not None

    def test_missing_fields_returns_error(self) -> None:
        error = validate_yaml("apiVersion: wrong\nkind: Other\n")
        assert error is not None


# ── 10. make_eval_context ───────────────────────────────────────────────

class TestMakeEvalContext:
    """Verify EvalContext construction from runtime params."""

    def test_defaults(self) -> None:
        ctx = make_eval_context(tool_name="run")
        assert ctx.tool == "run"
        assert ctx.mode == "interactive"
        assert ctx.model == ""
        assert ctx.mcp_server == ""

    def test_all_fields(self) -> None:
        ctx = make_eval_context(
            tool_name="bash",
            mcp_server="my-server",
            execution_context="background",
            model="gpt-4.1",
        )
        assert ctx.tool == "bash"
        assert ctx.mode == "background"
        assert ctx.model == "gpt-4.1"
        assert ctx.mcp_server == "my-server"
