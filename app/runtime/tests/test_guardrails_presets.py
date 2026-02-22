"""Tests for guardrails preset system -- model tiers, risk levels, and policy presets."""

from __future__ import annotations

from app.runtime.state.guardrails_config import (
    _ALL_PRESET_TOOL_IDS,
    _MODEL_TIERS,
    PRESET_BALANCED,
    PRESET_PERMISSIVE,
    PRESET_RESTRICTIVE,
    GuardrailsConfigStore,
    _build_preset_policies,
    _risk_of,
    get_model_tier,
    get_preset_for_model,
    list_presets,
)


class TestModelTiers:
    def test_strong_models_are_tier_1(self) -> None:
        assert get_model_tier("gpt-5.3-codex") == 1
        assert get_model_tier("claude-opus-4.6") == 1
        assert get_model_tier("claude-opus-4.6-fast") == 1

    def test_standard_models_are_tier_2(self) -> None:
        assert get_model_tier("claude-sonnet-4.6") == 2
        assert get_model_tier("gpt-5.2") == 2
        assert get_model_tier("gemini-3-pro-preview") == 2

    def test_cautious_models_are_tier_3(self) -> None:
        assert get_model_tier("gpt-4.1") == 3
        assert get_model_tier("gpt-5-mini") == 3

    def test_unknown_model_defaults_to_tier_3(self) -> None:
        assert get_model_tier("some-future-model") == 3

    def test_tier_to_preset(self) -> None:
        assert get_preset_for_model("gpt-5.3-codex") == PRESET_PERMISSIVE
        assert get_preset_for_model("claude-sonnet-4.6") == PRESET_BALANCED
        assert get_preset_for_model("gpt-4.1") == PRESET_RESTRICTIVE
        assert get_preset_for_model("unknown") == PRESET_RESTRICTIVE


class TestRiskClassification:
    """Verify the risk level assigned to different tool/MCP/skill IDs."""

    def test_read_only_sdk_tools_are_low(self) -> None:
        for t in ("view", "grep", "glob"):
            assert _risk_of(t) == "low"

    def test_file_write_sdk_tools_are_medium(self) -> None:
        assert _risk_of("create") == "medium"
        assert _risk_of("edit") == "medium"

    def test_terminal_tools_are_high(self) -> None:
        assert _risk_of("run") == "high"
        assert _risk_of("bash") == "high"

    def test_mslearn_mcp_is_low(self) -> None:
        assert _risk_of("mcp:microsoft-learn") == "low"

    def test_playwright_mcp_is_medium(self) -> None:
        assert _risk_of("mcp:playwright") == "medium"

    def test_github_azure_mcp_are_high(self) -> None:
        assert _risk_of("mcp:github-mcp-server") == "high"
        assert _risk_of("mcp:azure-mcp-server") == "high"

    def test_read_only_skills_are_low(self) -> None:
        for s in ("skill:daily-briefing", "skill:wiki-search", "skill:gh-status-check"):
            assert _risk_of(s) == "low"

    def test_browser_skills_are_medium(self) -> None:
        assert _risk_of("skill:web-search") == "medium"
        assert _risk_of("skill:summarize-url") == "medium"

    def test_foundry_skills_are_high(self) -> None:
        assert _risk_of("skill:setup-foundry") == "high"
        assert _risk_of("skill:foundry-agent-chat") == "high"

    def test_unknown_mcp_defaults_to_high(self) -> None:
        assert _risk_of("mcp:some-custom-server") == "high"

    def test_voice_call_is_high(self) -> None:
        assert _risk_of("make_voice_call") == "high"

    def test_card_tools_are_low(self) -> None:
        assert _risk_of("send_adaptive_card") == "low"
        assert _risk_of("send_hero_card") == "low"


class TestPresetPolicies:
    # ── Restrictive ──
    def test_restrictive_denies_terminal_in_background(self) -> None:
        p = _build_preset_policies(PRESET_RESTRICTIVE)
        assert p["tool_policies"]["background"]["run"] == "deny"
        assert p["tool_policies"]["background"]["bash"] == "deny"

    def test_restrictive_hitl_for_edits_in_interactive(self) -> None:
        p = _build_preset_policies(PRESET_RESTRICTIVE)
        assert p["tool_policies"]["interactive"]["edit"] == "hitl"
        assert p["tool_policies"]["interactive"]["create"] == "hitl"

    def test_restrictive_filters_read_only_everywhere(self) -> None:
        p = _build_preset_policies(PRESET_RESTRICTIVE)
        for ctx in ("interactive", "background"):
            assert p["tool_policies"][ctx]["view"] == "filter"
            assert p["tool_policies"][ctx]["grep"] == "filter"
            assert p["tool_policies"][ctx]["mcp:microsoft-learn"] == "filter"

    def test_restrictive_denies_github_azure_in_background(self) -> None:
        p = _build_preset_policies(PRESET_RESTRICTIVE)
        assert p["tool_policies"]["background"]["mcp:github-mcp-server"] == "deny"
        assert p["tool_policies"]["background"]["mcp:azure-mcp-server"] == "deny"

    def test_restrictive_hitl_github_azure_in_interactive(self) -> None:
        p = _build_preset_policies(PRESET_RESTRICTIVE)
        assert p["tool_policies"]["interactive"]["mcp:github-mcp-server"] == "hitl"
        assert p["tool_policies"]["interactive"]["mcp:azure-mcp-server"] == "hitl"

    def test_restrictive_denies_browser_in_background(self) -> None:
        p = _build_preset_policies(PRESET_RESTRICTIVE)
        assert p["tool_policies"]["background"]["mcp:playwright"] == "deny"

    # ── Balanced ──
    def test_balanced_filters_low_risk_everywhere(self) -> None:
        p = _build_preset_policies(PRESET_BALANCED)
        for ctx in ("interactive", "background"):
            assert p["tool_policies"][ctx]["view"] == "filter"
            assert p["tool_policies"][ctx]["mcp:microsoft-learn"] == "filter"
            assert p["tool_policies"][ctx]["list_scheduled_tasks"] == "filter"

    def test_balanced_hitl_terminal_in_interactive(self) -> None:
        p = _build_preset_policies(PRESET_BALANCED)
        assert p["tool_policies"]["interactive"]["run"] == "hitl"
        assert p["tool_policies"]["interactive"]["bash"] == "hitl"

    def test_balanced_denies_high_risk_in_background(self) -> None:
        p = _build_preset_policies(PRESET_BALANCED)
        assert p["tool_policies"]["background"]["run"] == "deny"
        assert p["tool_policies"]["background"]["bash"] == "deny"
        assert p["tool_policies"]["background"]["mcp:github-mcp-server"] == "deny"
        assert p["tool_policies"]["background"]["mcp:azure-mcp-server"] == "deny"

    def test_balanced_hitl_browser_in_background(self) -> None:
        p = _build_preset_policies(PRESET_BALANCED)
        assert p["tool_policies"]["background"]["mcp:playwright"] == "hitl"

    def test_balanced_filters_file_ops_in_interactive(self) -> None:
        p = _build_preset_policies(PRESET_BALANCED)
        assert p["tool_policies"]["interactive"]["create"] == "filter"
        assert p["tool_policies"]["interactive"]["edit"] == "filter"

    def test_balanced_filters_file_ops_in_background(self) -> None:
        p = _build_preset_policies(PRESET_BALANCED)
        assert p["tool_policies"]["background"]["create"] == "filter"
        assert p["tool_policies"]["background"]["edit"] == "filter"

    # ── Permissive ──
    def test_permissive_filters_most_in_interactive(self) -> None:
        p = _build_preset_policies(PRESET_PERMISSIVE)
        for tool in ("create", "edit", "view", "grep", "glob", "run", "bash"):
            assert p["tool_policies"]["interactive"][tool] == "filter"
        assert p["tool_policies"]["interactive"]["mcp:github-mcp-server"] == "filter"
        assert p["tool_policies"]["interactive"]["mcp:azure-mcp-server"] == "filter"

    def test_permissive_hitl_high_risk_in_background(self) -> None:
        p = _build_preset_policies(PRESET_PERMISSIVE)
        assert p["tool_policies"]["background"]["run"] == "hitl"
        assert p["tool_policies"]["background"]["bash"] == "hitl"
        assert p["tool_policies"]["background"]["mcp:github-mcp-server"] == "hitl"
        assert p["tool_policies"]["background"]["mcp:azure-mcp-server"] == "hitl"

    def test_permissive_filters_files_and_browser_in_background(self) -> None:
        p = _build_preset_policies(PRESET_PERMISSIVE)
        assert p["tool_policies"]["background"]["create"] == "filter"
        assert p["tool_policies"]["background"]["edit"] == "filter"
        assert p["tool_policies"]["background"]["mcp:playwright"] == "filter"
        assert p["tool_policies"]["background"]["mcp:microsoft-learn"] == "filter"


class TestListPresets:
    def test_returns_three_presets(self) -> None:
        presets = list_presets()
        assert len(presets) == 3
        ids = {p["id"] for p in presets}
        assert ids == {"restrictive", "balanced", "permissive"}

    def test_each_preset_has_recommended_models(self) -> None:
        for p in list_presets():
            assert len(p["recommended_for"]) > 0
            assert isinstance(p["tier"], int)


class TestApplyPreset:
    def test_apply_preset_sets_policies(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.apply_preset(PRESET_BALANCED)
        assert store.config.hitl_enabled is True
        assert store.config.context_defaults["interactive"] == "filter"
        assert store.config.context_defaults["background"] == "hitl"
        assert store.config.tool_policies["interactive"]["run"] == "hitl"

    def test_apply_preset_invalid_raises(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        try:
            store.apply_preset("invalid")
            assert False, "should have raised ValueError"
        except ValueError:
            pass

    def test_apply_model_defaults_adds_columns(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.apply_model_defaults(["gpt-5.3-codex", "gpt-4.1"])
        assert "gpt-5.3-codex" in store.config.model_columns
        assert "gpt-4.1" in store.config.model_columns

    def test_apply_model_defaults_differentiates_tiers(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.apply_model_defaults(["gpt-5.3-codex", "gpt-5.2", "gpt-4.1"])

        strong = store.config.model_policies["gpt-5.3-codex"]
        standard = store.config.model_policies["gpt-5.2"]
        cautious = store.config.model_policies["gpt-4.1"]

        # Strong (permissive): view filtered everywhere, run filtered interactive / hitl bg
        assert strong["interactive"]["view"] == "filter"
        assert strong["interactive"]["run"] == "filter"
        assert strong["background"]["run"] == "hitl"
        assert strong["interactive"]["mcp:microsoft-learn"] == "filter"

        # Standard (balanced): view filtered, run hitl interactive / deny bg
        assert standard["interactive"]["view"] == "filter"
        assert standard["interactive"]["run"] == "hitl"
        assert standard["background"]["run"] == "deny"
        assert standard["interactive"]["mcp:microsoft-learn"] == "filter"

        # Cautious (restrictive): run hitl interactive / deny bg, github deny bg
        assert cautious["interactive"]["run"] == "hitl"
        assert cautious["background"]["run"] == "deny"
        assert cautious["interactive"]["bash"] == "hitl"
        assert cautious["background"]["bash"] == "deny"
        assert cautious["background"]["mcp:github-mcp-server"] == "deny"
        assert cautious["interactive"]["mcp:microsoft-learn"] == "filter"

    def test_mcp_risk_differentiation_in_model_policies(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.apply_model_defaults(["gpt-5.3-codex"])
        strong = store.config.model_policies["gpt-5.3-codex"]
        # MS Learn (low risk) -> filter everywhere
        assert strong["interactive"]["mcp:microsoft-learn"] == "filter"
        assert strong["background"]["mcp:microsoft-learn"] == "filter"
        # Playwright (medium risk) -> filter everywhere (permissive)
        assert strong["interactive"]["mcp:playwright"] == "filter"
        assert strong["background"]["mcp:playwright"] == "filter"
        # GitHub/Azure (high risk) -> filter interactive / hitl background
        assert strong["interactive"]["mcp:github-mcp-server"] == "filter"
        assert strong["background"]["mcp:github-mcp-server"] == "hitl"
        assert strong["interactive"]["mcp:azure-mcp-server"] == "filter"
        assert strong["background"]["mcp:azure-mcp-server"] == "hitl"

    def test_cautious_model_mcp_risk_differentiation(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.apply_model_defaults(["gpt-4.1"])
        cautious = store.config.model_policies["gpt-4.1"]
        # MS Learn (low risk) -> filter everywhere
        assert cautious["interactive"]["mcp:microsoft-learn"] == "filter"
        assert cautious["background"]["mcp:microsoft-learn"] == "filter"
        # Playwright (medium risk) -> hitl interactive / deny background
        assert cautious["interactive"]["mcp:playwright"] == "hitl"
        assert cautious["background"]["mcp:playwright"] == "deny"
        # GitHub/Azure (high risk) -> hitl interactive / deny background
        assert cautious["interactive"]["mcp:github-mcp-server"] == "hitl"
        assert cautious["background"]["mcp:github-mcp-server"] == "deny"
        assert cautious["interactive"]["mcp:azure-mcp-server"] == "hitl"
        assert cautious["background"]["mcp:azure-mcp-server"] == "deny"

    def test_resolve_action_uses_model_policy(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_hitl_enabled(True)
        store.apply_model_defaults(["gpt-4.1"])
        # gpt-4.1 is tier 3 (restrictive) -- run in interactive should be hitl
        result = store.resolve_action(
            "run", execution_context="interactive", model="gpt-4.1",
        )
        assert result == "hitl"
        # run in background should be deny
        result = store.resolve_action(
            "run", execution_context="background", model="gpt-4.1",
        )
        assert result == "deny"

    def test_resolve_action_mslearn_allowed_for_cautious(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_hitl_enabled(True)
        store.apply_model_defaults(["gpt-4.1"])
        result = store.resolve_action(
            "mcp:microsoft-learn", mcp_server="microsoft-learn", model="gpt-4.1",
        )
        # MS Learn is low risk, filtered even for cautious models
        assert result == "filter"

    def test_preset_populates_mcp_and_skills(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.apply_preset(PRESET_BALANCED)
        policies = store.config.tool_policies
        # MCP servers should be in the policies
        assert "mcp:microsoft-learn" in policies["interactive"]
        assert "mcp:github-mcp-server" in policies["interactive"]
        # Skills should be in the policies
        assert "skill:web-search" in policies["interactive"]
        assert "skill:daily-briefing" in policies["interactive"]


class TestSetAllStrategies:
    """Tests for the bulk set_all_strategies method."""

    def test_sets_all_tool_policies_to_strategy(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_all_strategies("hitl")
        for ctx in ("interactive", "background"):
            for tool_id in _ALL_PRESET_TOOL_IDS:
                assert store.config.tool_policies[ctx][tool_id] == "hitl"

    def test_sets_context_defaults(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_all_strategies("pitl")
        assert store.config.context_defaults["interactive"] == "pitl"
        assert store.config.context_defaults["background"] == "pitl"

    def test_populates_all_model_columns_and_policies(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_all_strategies("aitl")
        # All known models should be present as model columns
        assert len(store.config.model_columns) == len(_MODEL_TIERS)
        for model in _MODEL_TIERS:
            assert model in store.config.model_columns
            for ctx in ("interactive", "background"):
                for tool_id in _ALL_PRESET_TOOL_IDS:
                    assert store.config.model_policies[model][ctx][tool_id] == "aitl"

    def test_enables_guardrails(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_hitl_enabled(False)
        store.set_all_strategies("filter")
        assert store.config.hitl_enabled is True

    def test_accepts_all_valid_strategies(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        for strategy in ("allow", "deny", "hitl", "pitl", "aitl", "filter", "ask"):
            store.set_all_strategies(strategy)
            assert store.config.context_defaults["interactive"] == strategy

    def test_rejects_invalid_strategy(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        try:
            store.set_all_strategies("invalid")
            assert False, "should have raised ValueError"
        except ValueError:
            pass


class TestBackgroundAgents:
    """Background agent metadata and resolve_action fallback."""

    def test_list_background_agents(self) -> None:
        from app.runtime.state.guardrails_config import list_background_agents

        agents = list_background_agents()
        ids = [a["id"] for a in agents]
        assert "scheduler" in ids
        assert "bot_processor" in ids
        assert "proactive_loop" in ids
        assert "memory_formation" in ids
        assert "aitl_reviewer" in ids
        for agent in agents:
            assert "name" in agent
            assert "description" in agent
            assert "has_tools" in agent
            assert "risk_note" in agent

    def test_resolve_scheduler_falls_back_to_background(self, tmp_path) -> None:
        """When no scheduler-specific policy exists, background applies."""
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_hitl_enabled(True)
        store.set_context_default("background", "hitl")
        assert store.resolve_action("run", execution_context="scheduler") == "hitl"

    def test_resolve_scheduler_override_takes_precedence(self, tmp_path) -> None:
        """Agent-specific context_default overrides background."""
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_hitl_enabled(True)
        store.set_context_default("background", "hitl")
        store.set_context_default("scheduler", "deny")
        assert store.resolve_action("run", execution_context="scheduler") == "deny"

    def test_resolve_bot_processor_falls_back_to_background(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_hitl_enabled(True)
        store.set_context_default("background", "filter")
        assert store.resolve_action("edit", execution_context="bot_processor") == "filter"

    def test_resolve_bot_processor_tool_policy_takes_precedence(self, tmp_path) -> None:
        """Agent-specific tool_policy beats background fallback."""
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_hitl_enabled(True)
        store.set_context_default("background", "filter")
        store.set_tool_policy("bot_processor", "run", "deny")
        assert store.resolve_action("run", execution_context="bot_processor") == "deny"
        # Other tools still fall back to background
        assert store.resolve_action("edit", execution_context="bot_processor") == "filter"

    def test_remove_context_default(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_context_default("scheduler", "deny")
        assert "scheduler" in store.config.context_defaults
        result = store.remove_context_default("scheduler")
        assert result is True
        assert "scheduler" not in store.config.context_defaults

    def test_remove_context_default_missing(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        result = store.remove_context_default("nonexistent")
        assert result is False
