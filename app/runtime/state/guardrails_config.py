"""Guardrails configuration -- HITL approval rules for tools and MCP servers."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
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

from .guardrails_bulk import (
    apply_model_defaults_to_config,
    apply_preset_to_config,
    set_all_strategies_on_config,
)

# Re-export public symbols so existing imports keep working.
from .guardrails_models import GuardrailRule, GuardrailsConfig, _VALID_STRATEGIES
from .guardrails_presets import (
    PRESET_BALANCED,
    PRESET_PERMISSIVE,
    PRESET_RESTRICTIVE,
    _ALL_PRESET_TOOL_IDS,
    _build_preset_policies,
    list_background_agents,
    list_presets,
)
from .guardrails_risk import (
    _MODEL_TIERS,
    _risk_of,
    get_model_tier,
    get_preset_for_model,
    list_model_tiers,
)

logger = logging.getLogger(__name__)

_instance: GuardrailsConfigStore | None = None


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

    def apply_preset(self, preset: str, *, auto_models: bool = True) -> None:
        """Apply a named preset to context_defaults and tool_policies."""
        apply_preset_to_config(self._config, preset, auto_models=auto_models)
        self._save()

    def set_all_strategies(self, strategy: str) -> None:
        """Set every tool policy and context default to *strategy*."""
        set_all_strategies_on_config(self._config, strategy)
        self._save()

    def apply_model_defaults(
        self,
        models: list[str] | None = None,
        *,
        preset: str | None = None,
    ) -> None:
        """Auto-populate model columns with tier-appropriate policies."""
        apply_model_defaults_to_config(self._config, models, preset=preset)
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
