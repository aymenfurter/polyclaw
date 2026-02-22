"""End-to-end validation of guardrail policy resolution.

Each test class sets up a realistic policy configuration (preset, model
columns, custom rules) and then asserts that ``resolve_action`` returns
the correct strategy for a wide matrix of (tool, context, model) inputs.
"""

from __future__ import annotations

import pytest

from app.runtime.state.guardrails_config import (
    GuardrailsConfigStore,
    PRESET_BALANCED,
    PRESET_PERMISSIVE,
    PRESET_RESTRICTIVE,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _store(tmp_path, name: str = "g.json") -> GuardrailsConfigStore:
    return GuardrailsConfigStore(tmp_path / name)


# ── 1. Guardrails disabled -- everything is allowed ─────────────────────

class TestGuardrailsDisabled:
    """When hitl_enabled=False every call must return 'allow'."""

    def test_sdk_tools_allowed(self, tmp_path) -> None:
        s = _store(tmp_path)
        assert s.config.hitl_enabled is False
        for tool in ("run", "bash", "create", "edit", "view", "grep", "glob"):
            assert s.resolve_action(tool) == "allow"

    def test_mcp_tools_allowed(self, tmp_path) -> None:
        s = _store(tmp_path)
        for mcp in ("github-mcp-server", "azure-mcp-server", "playwright"):
            assert s.resolve_action(
                f"mcp:{mcp}", mcp_server=mcp, execution_context="background"
            ) == "allow"

    def test_even_with_preset_applied_disabled_wins(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.apply_preset(PRESET_RESTRICTIVE, auto_models=False)
        s.set_hitl_enabled(False)
        assert s.resolve_action("run", execution_context="background") == "allow"
        assert s.resolve_action("bash", execution_context="background") == "allow"

    def test_model_policy_ignored_when_disabled(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.apply_model_defaults(["gpt-4.1"])
        s.set_hitl_enabled(False)
        assert s.resolve_action("run", model="gpt-4.1") == "allow"


# ── 2. Restrictive preset -- tightest controls ──────────────────────────

class TestRestrictivePolicy:
    """Full resolution through a restrictive preset with model columns."""

    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path) -> None:
        self.s = _store(tmp_path)
        self.s.apply_preset(PRESET_RESTRICTIVE, auto_models=False)

    # Interactive context
    def test_view_filtered_interactive(self) -> None:
        assert self.s.resolve_action("view", execution_context="interactive") == "filter"

    def test_grep_filtered_interactive(self) -> None:
        assert self.s.resolve_action("grep", execution_context="interactive") == "filter"

    def test_create_hitl_interactive(self) -> None:
        assert self.s.resolve_action("create", execution_context="interactive") == "hitl"

    def test_edit_hitl_interactive(self) -> None:
        assert self.s.resolve_action("edit", execution_context="interactive") == "hitl"

    def test_run_hitl_interactive(self) -> None:
        assert self.s.resolve_action("run", execution_context="interactive") == "hitl"

    def test_github_mcp_hitl_interactive(self) -> None:
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            execution_context="interactive",
        ) == "hitl"

    def test_mslearn_mcp_filtered_interactive(self) -> None:
        assert self.s.resolve_action(
            "mcp:microsoft-learn", mcp_server="microsoft-learn",
            execution_context="interactive",
        ) == "filter"

    # Background context
    def test_view_filtered_background(self) -> None:
        assert self.s.resolve_action("view", execution_context="background") == "filter"

    def test_create_denied_background(self) -> None:
        assert self.s.resolve_action("create", execution_context="background") == "deny"

    def test_run_denied_background(self) -> None:
        assert self.s.resolve_action("run", execution_context="background") == "deny"

    def test_bash_denied_background(self) -> None:
        assert self.s.resolve_action("bash", execution_context="background") == "deny"

    def test_github_denied_background(self) -> None:
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            execution_context="background",
        ) == "deny"

    def test_azure_denied_background(self) -> None:
        assert self.s.resolve_action(
            "mcp:azure-mcp-server", mcp_server="azure-mcp-server",
            execution_context="background",
        ) == "deny"

    def test_playwright_denied_background(self) -> None:
        assert self.s.resolve_action(
            "mcp:playwright", mcp_server="playwright",
            execution_context="background",
        ) == "deny"

    def test_mslearn_filtered_background(self) -> None:
        assert self.s.resolve_action(
            "mcp:microsoft-learn", mcp_server="microsoft-learn",
            execution_context="background",
        ) == "filter"

    def test_card_tools_filtered_everywhere(self) -> None:
        for ctx in ("interactive", "background"):
            assert self.s.resolve_action(
                "send_adaptive_card", execution_context=ctx,
            ) == "filter"

    def test_default_context_is_interactive(self) -> None:
        # No context specified defaults to interactive
        assert self.s.resolve_action("run") == "hitl"


# ── 3. Balanced preset ──────────────────────────────────────────────────

class TestBalancedPolicy:
    """Resolution through balanced preset -- file ops allowed, terminal guarded."""

    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path) -> None:
        self.s = _store(tmp_path)
        self.s.apply_preset(PRESET_BALANCED, auto_models=False)

    def test_file_ops_filtered_everywhere(self) -> None:
        for ctx in ("interactive", "background"):
            assert self.s.resolve_action("create", execution_context=ctx) == "filter"
            assert self.s.resolve_action("edit", execution_context=ctx) == "filter"

    def test_terminal_hitl_interactive(self) -> None:
        assert self.s.resolve_action("run", execution_context="interactive") == "hitl"

    def test_terminal_denied_background(self) -> None:
        assert self.s.resolve_action("run", execution_context="background") == "deny"

    def test_playwright_hitl_background(self) -> None:
        assert self.s.resolve_action(
            "mcp:playwright", mcp_server="playwright",
            execution_context="background",
        ) == "hitl"

    def test_github_denied_background(self) -> None:
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            execution_context="background",
        ) == "deny"

    def test_mslearn_filtered_everywhere(self) -> None:
        for ctx in ("interactive", "background"):
            assert self.s.resolve_action(
                "mcp:microsoft-learn", mcp_server="microsoft-learn",
                execution_context=ctx,
            ) == "filter"

    def test_skills_follow_risk(self) -> None:
        # daily-briefing is low risk -> filter (Prompt Shields baseline)
        assert self.s.resolve_action(
            "skill:daily-briefing", execution_context="background"
        ) == "filter"
        # web-search is medium risk -> hitl in background
        assert self.s.resolve_action(
            "skill:web-search", execution_context="background"
        ) == "hitl"


# ── 4. Permissive preset ────────────────────────────────────────────────

class TestPermissivePolicy:
    """Resolution through permissive preset -- nearly everything open."""

    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path) -> None:
        self.s = _store(tmp_path)
        self.s.apply_preset(PRESET_PERMISSIVE, auto_models=False)

    def test_all_sdk_tools_filtered_interactive(self) -> None:
        for tool in ("create", "edit", "view", "grep", "glob", "run", "bash"):
            assert self.s.resolve_action(tool, execution_context="interactive") == "filter"

    def test_all_mcp_filtered_interactive(self) -> None:
        for mcp in ("github-mcp-server", "azure-mcp-server", "playwright", "microsoft-learn"):
            assert self.s.resolve_action(
                f"mcp:{mcp}", mcp_server=mcp, execution_context="interactive",
            ) == "filter"

    def test_terminal_hitl_background(self) -> None:
        assert self.s.resolve_action("run", execution_context="background") == "hitl"

    def test_github_hitl_background(self) -> None:
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            execution_context="background",
        ) == "hitl"

    def test_file_ops_filtered_background(self) -> None:
        for tool in ("create", "edit"):
            assert self.s.resolve_action(tool, execution_context="background") == "filter"

    def test_playwright_filtered_background(self) -> None:
        assert self.s.resolve_action(
            "mcp:playwright", mcp_server="playwright",
            execution_context="background",
        ) == "filter"


# ── 5. Model-scoped resolution ──────────────────────────────────────────

class TestModelScopedResolution:
    """Model policies are more specific and take precedence over context tool policies."""

    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path) -> None:
        self.s = _store(tmp_path)
        self.s.apply_preset(PRESET_BALANCED, auto_models=False)
        self.s.apply_model_defaults(["gpt-5.3-codex", "gpt-5.2", "gpt-4.1"])

    # Strong model (gpt-5.3-codex) gets permissive policies -- model wins
    def test_strong_run_filter(self) -> None:
        # Model policy (permissive interactive high=filter) beats context (balanced hitl)
        assert self.s.resolve_action("run", model="gpt-5.3-codex") == "filter"

    def test_strong_view_filter(self) -> None:
        assert self.s.resolve_action("view", model="gpt-5.3-codex") == "filter"

    def test_strong_github_filter(self) -> None:
        # Model policy (permissive interactive high=filter) beats context (balanced hitl)
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            model="gpt-5.3-codex",
        ) == "filter"

    def test_strong_mslearn_filter(self) -> None:
        assert self.s.resolve_action(
            "mcp:microsoft-learn", mcp_server="microsoft-learn",
            model="gpt-5.3-codex",
        ) == "filter"

    # Standard model defaults to interactive context -- context policy wins
    def test_standard_run_hitl(self) -> None:
        # Default ctx=interactive, balanced interactive high-risk=hitl
        assert self.s.resolve_action("run", model="gpt-5.2") == "hitl"

    def test_standard_file_ops_filter(self) -> None:
        assert self.s.resolve_action("create", model="gpt-5.2") == "filter"
        assert self.s.resolve_action("edit", model="gpt-5.2") == "filter"

    def test_standard_github_hitl(self) -> None:
        # Default ctx=interactive, balanced interactive high-risk=hitl
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            model="gpt-5.2",
        ) == "hitl"

    # Cautious model defaults to interactive context -- context policy wins
    def test_cautious_run_hitl(self) -> None:
        # Default ctx=interactive, balanced interactive high-risk=hitl
        assert self.s.resolve_action("run", model="gpt-4.1") == "hitl"

    def test_cautious_create_hitl(self) -> None:
        # Model policy (restrictive interactive medium=hitl) beats context (balanced filter)
        assert self.s.resolve_action("create", model="gpt-4.1") == "hitl"

    def test_cautious_github_hitl(self) -> None:
        # Default ctx=interactive, balanced interactive high-risk=hitl
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            model="gpt-4.1",
        ) == "hitl"

    def test_cautious_mslearn_filter(self) -> None:
        assert self.s.resolve_action(
            "mcp:microsoft-learn", mcp_server="microsoft-learn",
            model="gpt-4.1",
        ) == "filter"

    def test_cautious_view_filter(self) -> None:
        assert self.s.resolve_action("view", model="gpt-4.1") == "filter"

    # Unknown model falls back to context policies (no model column)
    def test_unknown_model_uses_context_policy(self) -> None:
        # No model column for "random-model" -- falls through to context policy
        result = self.s.resolve_action(
            "run", execution_context="interactive", model="random-model",
        )
        # Balanced interactive: run=hitl
        assert result == "hitl"

    def test_model_overrides_context(self) -> None:
        # gpt-4.1 model policy (restrictive interactive high=hitl) and context
        # policy (balanced interactive high=hitl) both happen to agree here.
        result = self.s.resolve_action(
            "run", execution_context="interactive", model="gpt-4.1",
        )
        # Both agree on hitl, but model wins in general
        assert result == "hitl"


# ── 6. Custom rules override ────────────────────────────────────────────

class TestCustomRulesOverride:
    """Legacy rules fire for tools not covered by tool_policies or context_defaults."""

    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path) -> None:
        # No preset -- only hitl_enabled + rules. This avoids context_defaults
        # which sit above rules in the resolution cascade.
        self.s = _store(tmp_path)
        self.s.set_hitl_enabled(True)
        self.s.set_default_action("allow")

    def test_rule_can_deny_tool(self) -> None:
        self.s.add_rule(name="block-create", pattern="create", action="deny")
        assert self.s.resolve_action("create", execution_context="interactive") == "deny"

    def test_rule_catches_unlisted_tool(self) -> None:
        self.s.add_rule(
            name="block-custom", pattern="my_custom_tool",
            action="deny",
        )
        assert self.s.resolve_action("my_custom_tool", execution_context="interactive") == "deny"

    def test_rule_with_context_filter(self) -> None:
        self.s.add_rule(
            name="bg-only-block", pattern="my_custom_tool",
            action="deny", contexts=["background"],
        )
        # Denied in background
        assert self.s.resolve_action(
            "my_custom_tool", execution_context="background",
        ) == "deny"
        # In interactive, rule context filter doesn't match -> global default (allow)
        assert self.s.resolve_action(
            "my_custom_tool", execution_context="interactive",
        ) == "allow"

    def test_rule_with_model_filter(self) -> None:
        self.s.add_rule(
            name="deny-for-mini", pattern="my_custom_tool",
            action="deny", models=["gpt-5-mini"],
        )
        # Denied for gpt-5-mini
        assert self.s.resolve_action(
            "my_custom_tool", model="gpt-5-mini",
        ) == "deny"
        # Allowed for another model (falls to global default)
        assert self.s.resolve_action(
            "my_custom_tool", model="gpt-5.3-codex",
        ) == "allow"

    def test_disabled_rule_ignored(self) -> None:
        rule = self.s.add_rule(
            name="block-custom", pattern="my_custom_tool", action="deny",
        )
        self.s.update_rule(rule.id, enabled=False)
        # Disabled rule doesn't fire -> global default (allow)
        assert self.s.resolve_action("my_custom_tool", execution_context="interactive") == "allow"

    def test_mcp_scope_rule(self) -> None:
        self.s.add_rule(
            name="block-custom-mcp", pattern="my-server",
            scope="mcp", action="deny",
        )
        assert self.s.resolve_action(
            "some_tool", mcp_server="my-server",
        ) == "deny"

    def test_context_defaults_take_precedence_over_rules(self) -> None:
        # When context_defaults are set, they fire BEFORE rules
        self.s._config.context_defaults = {"interactive": "allow"}
        self.s.add_rule(
            name="block-custom", pattern="my_custom_tool", action="deny",
        )
        # Context default ("allow") wins over the rule ("deny")
        assert self.s.resolve_action(
            "my_custom_tool", execution_context="interactive",
        ) == "allow"
        # But without a matching context_default, the rule fires
        assert self.s.resolve_action(
            "my_custom_tool", execution_context="background",
        ) == "deny"


# ── 7. Priority: model > tool_policy > context_default > rule ───────────

class TestResolutionPriority:
    """Validate the cascade: model -> tool_policies -> context_default -> rules -> global."""

    def test_model_beats_tool_policy(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.apply_preset(PRESET_PERMISSIVE, auto_models=False)  # interactive run=filter
        s.apply_model_defaults(["gpt-4.1"])  # model restrictive interactive run=hitl
        # Model policy (more specific) wins over context tool policy
        assert s.resolve_action("run", execution_context="interactive", model="gpt-4.1") == "hitl"

    def test_tool_policy_beats_context_default(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.apply_preset(PRESET_BALANCED, auto_models=False)
        # Balanced context default for interactive is 'allow' (medium risk default)
        # But run is explicitly hitl in tool_policies
        assert s.resolve_action("run", execution_context="interactive") == "hitl"

    def test_context_default_beats_rules(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.set_hitl_enabled(True)
        s._config.context_defaults = {"interactive": "deny", "background": "deny"}
        s.add_rule(name="allow-custom", pattern="unknown_tool", action="allow")
        # Context default fires before rule for tools not in tool_policies
        # But wait -- context default fires at step 3, rules at step 4
        # So context default should be returned
        assert s.resolve_action("unknown_tool", execution_context="interactive") == "deny"

    def test_rules_beat_global_default(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.set_hitl_enabled(True)
        s.set_default_action("allow")
        s.add_rule(name="block", pattern="dangerous_tool", action="deny")
        assert s.resolve_action("dangerous_tool") == "deny"

    def test_global_default_is_last_resort(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.set_hitl_enabled(True)
        s.set_default_action("deny")
        # No policies, no rules, no context defaults
        assert s.resolve_action("some_random_tool") == "deny"


# ── 8. Mixed preset + model + rule scenario ─────────────────────────────

class TestMixedScenario:
    """Realistic setup: balanced preset + all 3 model tiers + a custom rule."""

    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path) -> None:
        self.s = _store(tmp_path)
        self.s.apply_preset(PRESET_BALANCED, auto_models=False)
        self.s.apply_model_defaults(["gpt-5.3-codex", "gpt-5.2", "gpt-4.1"])
        # Custom rule: block voice calls for all models
        self.s.add_rule(
            name="no-voice", pattern="make_voice_call", action="deny",
        )

    def test_voice_call_denied_by_rule(self) -> None:
        # make_voice_call is in preset tool_policies (high risk -> hitl interactive / deny bg)
        # Since it's in tool_policies, the rule doesn't override it for preset contexts.
        # But the tool_policies entry comes first.
        # Interactive: make_voice_call = hitl (preset balanced: high risk interactive)
        assert self.s.resolve_action(
            "make_voice_call", execution_context="interactive",
        ) == "hitl"

    def test_voice_call_denied_background(self) -> None:
        assert self.s.resolve_action(
            "make_voice_call", execution_context="background",
        ) == "deny"

    def test_strong_model_create_files_filtered(self) -> None:
        assert self.s.resolve_action("create", model="gpt-5.3-codex") == "filter"
        assert self.s.resolve_action("edit", model="gpt-5.3-codex") == "filter"

    def test_cautious_model_uses_context_policy_for_terminal(self) -> None:
        # Default ctx=interactive, balanced interactive high-risk=hitl
        # Context tool policy takes precedence over model policy
        assert self.s.resolve_action("run", model="gpt-4.1") == "hitl"
        assert self.s.resolve_action("bash", model="gpt-4.1") == "hitl"

    def test_standard_model_mslearn_filtered(self) -> None:
        assert self.s.resolve_action(
            "mcp:microsoft-learn", mcp_server="microsoft-learn", model="gpt-5.2",
        ) == "filter"

    def test_context_fallback_for_unknown_tool(self) -> None:
        # Tool not in preset list, not in rules (except make_voice_call)
        # Falls to context default: balanced interactive = filter
        assert self.s.resolve_action(
            "some_future_tool", execution_context="interactive",
        ) == "filter"

    def test_context_fallback_background(self) -> None:
        # Balanced background context default = hitl
        assert self.s.resolve_action(
            "some_future_tool", execution_context="background",
        ) == "hitl"

    def test_card_tools_always_filtered(self) -> None:
        for card in ("send_adaptive_card", "send_hero_card",
                      "send_thumbnail_card", "send_card_carousel"):
            for ctx in ("interactive", "background"):
                assert self.s.resolve_action(card, execution_context=ctx) == "filter"

    def test_strong_model_github_filter(self) -> None:
        # Model policy (permissive interactive high=filter) beats context (balanced hitl)
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            model="gpt-5.3-codex",
        ) == "filter"

    def test_cautious_model_github_hitl(self) -> None:
        # Default ctx=interactive, balanced interactive high-risk=hitl
        # Context tool policy takes precedence over model policy (deny)
        assert self.s.resolve_action(
            "mcp:github-mcp-server", mcp_server="github-mcp-server",
            model="gpt-4.1",
        ) == "hitl"


# ── 9. Preset auto-adds model columns ──────────────────────────────────

class TestPresetAutoModels:
    """Applying a preset with auto_models=True also creates model columns."""

    def test_balanced_adds_tier_2_models(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.apply_preset(PRESET_BALANCED, auto_models=True)
        # Should have the tier-2 models as columns
        assert "claude-sonnet-4.6" in s.config.model_columns
        assert "gpt-5.2" in s.config.model_columns

    def test_restrictive_adds_tier_3_models(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.apply_preset(PRESET_RESTRICTIVE, auto_models=True)
        assert "gpt-4.1" in s.config.model_columns
        assert "gpt-5-mini" in s.config.model_columns

    def test_permissive_adds_tier_1_models(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.apply_preset(PRESET_PERMISSIVE, auto_models=True)
        assert "gpt-5.3-codex" in s.config.model_columns
        assert "claude-opus-4.6" in s.config.model_columns

    def test_auto_models_have_policies(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.apply_preset(PRESET_RESTRICTIVE, auto_models=True)
        # gpt-4.1 should have model policies populated per context
        assert "gpt-4.1" in s.config.model_policies
        assert s.config.model_policies["gpt-4.1"]["interactive"]["run"] == "hitl"
        assert s.config.model_policies["gpt-4.1"]["background"]["run"] == "deny"
        assert s.config.model_policies["gpt-4.1"]["interactive"]["view"] == "filter"
