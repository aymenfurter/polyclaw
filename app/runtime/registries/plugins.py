"""Plugin registry -- discover, install, and manage bundled plugins."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config.settings import cfg
from ..state.plugin_config import PluginConfigStore
from ..util.singletons import Singleton

logger = logging.getLogger(__name__)


@dataclass
class PluginManifest:
    id: str
    name: str
    description: str = ""
    version: str = "0.1.0"
    author: str = ""
    homepage: str = ""
    icon: str = ""
    default_enabled: bool = False
    setup_skill: str = ""
    setup_message: str = ""
    skills: list[str] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    source_dir: Path = field(default_factory=lambda: Path("."))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "author": self.author,
            "homepage": self.homepage,
            "icon": self.icon,
            "default_enabled": self.default_enabled,
            "setup_skill": self.setup_skill,
            "setup_message": self.setup_message,
            "skills": self.skills,
            "dependencies": self.dependencies,
        }


def _parse_manifest(manifest_path: Path) -> PluginManifest | None:
    try:
        raw = json.loads(manifest_path.read_text())
        skills_dir = manifest_path.parent / "skills"
        skill_names: list[str] = raw.get("skills", [])
        if not skill_names and skills_dir.is_dir():
            for d in sorted(skills_dir.iterdir()):
                if d.is_dir() and (d / "SKILL.md").exists():
                    skill_names.append(d.name)

        return PluginManifest(
            id=raw.get("id", manifest_path.parent.name),
            name=raw.get("name", manifest_path.parent.name),
            description=raw.get("description", ""),
            version=raw.get("version", "0.1.0"),
            author=raw.get("author", ""),
            homepage=raw.get("homepage", ""),
            icon=raw.get("icon", ""),
            default_enabled=raw.get("default_enabled", False),
            setup_skill=raw.get("setup_skill", ""),
            setup_message=raw.get("setup_message", ""),
            skills=skill_names,
            dependencies=raw.get("dependencies", {}),
            source_dir=manifest_path.parent,
        )
    except Exception as exc:
        logger.warning("Failed to parse plugin manifest %s: %s", manifest_path, exc)
        return None


class PluginRegistry:
    def __init__(self, store: PluginConfigStore | None = None) -> None:
        self._store = store or PluginConfigStore()
        self._plugins: dict[str, PluginManifest] = {}
        self._discover()

    @property
    def store(self) -> PluginConfigStore:
        return self._store

    def _discover(self) -> None:
        self._plugins.clear()
        for search_dir in (cfg.project_root / "plugins", cfg.data_dir / "plugins"):
            if not search_dir.is_dir():
                continue
            for d in sorted(search_dir.iterdir()):
                manifest_path = d / "PLUGIN.json"
                if d.is_dir() and manifest_path.exists():
                    manifest = _parse_manifest(manifest_path)
                    if manifest and manifest.id not in self._plugins:
                        self._plugins[manifest.id] = manifest
                        logger.info(
                            "  plugin: %s (%s) -- %d skill(s)",
                            manifest.id, manifest.name, len(manifest.skills),
                        )
        logger.info("Discovered %d plugin(s)", len(self._plugins))

    def refresh(self) -> None:
        self._discover()

    def list_plugins(self) -> list[dict[str, Any]]:
        result = []
        for plugin_id, manifest in self._plugins.items():
            state = self._store.get_state(plugin_id)
            entry = manifest.to_dict()
            entry["enabled"] = state.get("enabled", manifest.default_enabled)
            entry["setup_completed"] = state.get("setup_completed", False)
            entry["installed_at"] = state.get("installed_at")
            entry["skill_count"] = len(manifest.skills)
            entry["source"] = (
                "bundled" if (cfg.project_root / "plugins" / plugin_id).is_dir() else "user"
            )
            result.append(entry)
        return result

    def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        manifest = self._plugins.get(plugin_id)
        if not manifest:
            return None
        state = self._store.get_state(plugin_id)
        entry = manifest.to_dict()
        entry["enabled"] = state.get("enabled", manifest.default_enabled)
        entry["setup_completed"] = state.get("setup_completed", False)
        entry["installed_at"] = state.get("installed_at")
        entry["skill_count"] = len(manifest.skills)
        entry["source"] = (
            "bundled" if (cfg.project_root / "plugins" / plugin_id).is_dir() else "user"
        )
        return entry

    def get_manifest(self, plugin_id: str) -> PluginManifest | None:
        return self._plugins.get(plugin_id)

    def enable_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        manifest = self._plugins.get(plugin_id)
        if not manifest:
            return None

        state = self._store.get_state(plugin_id)
        setup_done = state.get("setup_completed", False)

        skills_dir = manifest.source_dir / "skills"
        if skills_dir.is_dir():
            for skill_dir in sorted(skills_dir.iterdir()):
                if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
                    continue
                if setup_done and skill_dir.name == manifest.setup_skill:
                    continue
                dest = cfg.user_skills_dir / skill_dir.name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill_dir, dest)
                logger.info("Installed plugin skill: %s -> %s", skill_dir.name, dest)

        self._store.set_enabled(plugin_id, True)
        return self.get_plugin(plugin_id)

    def disable_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        manifest = self._plugins.get(plugin_id)
        if not manifest:
            return None

        for skill_name in manifest.skills:
            skill_path = cfg.user_skills_dir / skill_name
            if skill_path.is_dir():
                shutil.rmtree(skill_path)
                logger.info("Removed plugin skill: %s", skill_name)

        if manifest.setup_skill:
            setup_path = cfg.user_skills_dir / manifest.setup_skill
            if setup_path.is_dir():
                shutil.rmtree(setup_path)

        self._store.set_enabled(plugin_id, False)
        return self.get_plugin(plugin_id)

    def get_setup_skill_content(self, plugin_id: str) -> str | None:
        manifest = self._plugins.get(plugin_id)
        if not manifest or not manifest.setup_skill:
            return None
        skill_path = manifest.source_dir / "skills" / manifest.setup_skill / "SKILL.md"
        return skill_path.read_text() if skill_path.exists() else None

    def complete_setup(self, plugin_id: str) -> dict[str, Any] | None:
        manifest = self._plugins.get(plugin_id)
        if not manifest:
            return None

        self._store.mark_setup_completed(plugin_id)

        if manifest.setup_skill:
            setup_path = cfg.user_skills_dir / manifest.setup_skill
            if setup_path.is_dir():
                shutil.rmtree(setup_path)

        return self.get_plugin(plugin_id)

    def import_from_zip(self, zip_path: Path) -> dict[str, Any] | None:
        import tempfile
        import zipfile

        user_plugins_dir = cfg.data_dir / "plugins"
        user_plugins_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            manifest_entries = [n for n in zf.namelist() if n.endswith("PLUGIN.json")]
            if not manifest_entries:
                raise ValueError("No PLUGIN.json found in zip archive")

            with tempfile.TemporaryDirectory() as tmpdir:
                zf.extractall(tmpdir)
                manifest_file = Path(tmpdir) / manifest_entries[0]
                plugin_root = manifest_file.parent

                manifest = _parse_manifest(manifest_file)
                if not manifest:
                    raise ValueError("Invalid PLUGIN.json in archive")

                dest = user_plugins_dir / manifest.id
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(plugin_root, dest)

        self._discover()
        return self.get_plugin(manifest.id)

    def remove_user_plugin(self, plugin_id: str) -> bool:
        user_path = cfg.data_dir / "plugins" / plugin_id
        if not user_path.is_dir():
            return False
        self.disable_plugin(plugin_id)
        shutil.rmtree(user_path)
        self._store.reset(plugin_id)
        self._discover()
        return True


get_plugin_registry, _reset_plugin_registry = Singleton.create(PluginRegistry)
