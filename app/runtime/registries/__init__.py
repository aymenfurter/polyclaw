"""Plugin and skill registries."""

from __future__ import annotations

from .plugins import PluginManifest, PluginRegistry, get_plugin_registry
from .skills import SkillInfo, SkillRegistry, get_registry

__all__ = [
    "PluginManifest",
    "PluginRegistry",
    "SkillInfo",
    "SkillRegistry",
    "get_plugin_registry",
    "get_registry",
]
