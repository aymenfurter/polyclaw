"""Bulk guardrails operations -- presets, strategies, and model defaults."""

from __future__ import annotations

from .models import GuardrailsConfig, _VALID_STRATEGIES
from .presets import (
    PRESET_BALANCED,
    PRESET_PERMISSIVE,
    PRESET_RESTRICTIVE,
    _ALL_PRESET_TOOL_IDS,
    _EFFECTIVE_MODEL_PRESET,
    _PRESET_MATRIX,
    _PRESET_OVERRIDES,
    _build_preset_policies,
    list_presets,
)
from .risk import (
    _MODEL_TIERS,
    _risk_of,
    get_model_tier,
    get_preset_for_model,
)


def apply_preset_to_config(
    config: GuardrailsConfig, preset: str, *, auto_models: bool = True,
) -> None:
    """Apply a named preset to *config* in place.

    Overwrites ``context_defaults`` and ``tool_policies``.  When
    *auto_models* is ``True``, recommended models are added as model
    columns with tier-appropriate policies and all existing model
    columns are refreshed.
    """
    valid = {PRESET_RESTRICTIVE, PRESET_BALANCED, PRESET_PERMISSIVE}
    if preset not in valid:
        raise ValueError("preset must be one of: %s" % ", ".join(sorted(valid)))
    policies = _build_preset_policies(preset)
    config.context_defaults = policies["context_defaults"]
    config.tool_policies = policies["tool_policies"]
    config.hitl_enabled = True
    if auto_models:
        preset_meta = next((p for p in list_presets() if p["id"] == preset), None)
        if preset_meta:
            new_models = [
                m for m in preset_meta["recommended_for"]
                if m not in config.model_columns
            ]
            if new_models:
                apply_model_defaults_to_config(config, new_models, preset=preset)
        if config.model_columns:
            apply_model_defaults_to_config(config, preset=preset)


def set_all_strategies_on_config(config: GuardrailsConfig, strategy: str) -> None:
    """Set every tool policy and context default on *config* to *strategy*.

    All tools in ``_ALL_PRESET_TOOL_IDS`` across interactive and background
    contexts are set to the given strategy.  All known models from
    ``_MODEL_TIERS`` are added as model columns with the same strategy.
    """
    if strategy not in _VALID_STRATEGIES:
        raise ValueError(
            "strategy must be one of: %s" % ", ".join(sorted(_VALID_STRATEGIES))
        )
    policies: dict[str, dict[str, str]] = {"interactive": {}, "background": {}}
    for tool_id in _ALL_PRESET_TOOL_IDS:
        for ctx in ("interactive", "background"):
            policies[ctx][tool_id] = strategy
    config.context_defaults = {
        "interactive": strategy,
        "background": strategy,
    }
    config.tool_policies = policies
    config.model_columns = sorted(_MODEL_TIERS.keys())
    model_policies: dict[str, dict[str, dict[str, str]]] = {}
    for model in config.model_columns:
        per_ctx: dict[str, dict[str, str]] = {}
        for ctx in ("interactive", "background"):
            per_ctx[ctx] = {tool_id: strategy for tool_id in _ALL_PRESET_TOOL_IDS}
        model_policies[model] = per_ctx
    config.model_policies = model_policies
    config.hitl_enabled = True


def apply_model_defaults_to_config(
    config: GuardrailsConfig,
    models: list[str] | None = None,
    *,
    preset: str | None = None,
) -> None:
    """Auto-populate model columns on *config* with tier-appropriate policies.

    For each model, determines the effective preset via the
    ``_EFFECTIVE_MODEL_PRESET`` cross-reference of *preset* (the
    user-selected risk posture) and the model's inherent tier.

    If *models* is ``None``, uses the existing ``model_columns``.
    If *preset* is ``None``, falls back to the model's own tier preset.
    """
    target_models = models if models is not None else list(config.model_columns)
    for model in target_models:
        if model not in config.model_columns:
            config.model_columns.append(model)
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
        config.model_policies[model] = per_ctx
