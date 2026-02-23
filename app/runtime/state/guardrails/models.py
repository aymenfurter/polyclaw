"""Guardrails data models -- dataclasses and shared constants."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

_VALID_STRATEGIES = frozenset({"allow", "deny", "hitl", "pitl", "aitl", "filter", "ask"})


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
