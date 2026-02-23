"""Skill registry -- list, install, and remove agent skills."""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config.settings import cfg
from ..util.singletons import register_singleton

logger = logging.getLogger(__name__)

_CURATED_SKILLS: set[str] = {"web-search", "summarize-url", "daily-briefing"}
_ORIGIN_FILE = ".origin"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_FIELD_RE = re.compile(r"^(\w+)\s*:\s*(.+)", re.MULTILINE)
_VERB_RE = re.compile(r"^\s+verb:\s*(.+)", re.MULTILINE)


@dataclass
class SkillInfo:
    name: str
    verb: str = ""
    description: str = ""
    source: str = ""
    category: str = ""
    repo_owner: str = ""
    repo_name: str = ""
    repo_path: str = ""
    repo_branch: str = "main"
    installed: bool = False
    edit_count: int = 0
    recommended: bool = False
    origin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verb": self.verb or self.name,
            "description": self.description,
            "source": self.source,
            "category": self.category,
            "installed": self.installed,
            "edit_count": self.edit_count,
            "recommended": self.recommended,
            "origin": self.origin,
        }


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    result: dict[str, str] = {}
    for fm in _FIELD_RE.finditer(m.group(1)):
        result[fm.group(1).strip()] = fm.group(2).strip().strip("'\"")
    # Extract nested verb from metadata block
    vm = _VERB_RE.search(m.group(1))
    if vm:
        result["verb"] = vm.group(1).strip().strip("'\"")
    return result


def _determine_origin(
    skill_dir: Path,
    builtin_names: set[str],
    plugin_skill_names: set[str],
) -> str:
    origin_file = skill_dir / _ORIGIN_FILE
    if origin_file.exists():
        try:
            return json.loads(origin_file.read_text()).get("origin", "marketplace")
        except Exception:
            return "marketplace"
    if skill_dir.name in plugin_skill_names:
        return "plugin"
    if skill_dir.name in builtin_names:
        return "built-in"
    return "agent-created"


class SkillRegistry:
    def __init__(self) -> None:
        self._catalog_cache: list[SkillInfo] | None = None
        self._catalog_ts: float = 0
        self.rate_limited: bool = False
        self.rate_limit_reset: int | None = None

    def get_skill_content(self, name: str) -> str | None:
        skill_file = cfg.user_skills_dir / name / "SKILL.md"
        return skill_file.read_text(errors="replace") if skill_file.exists() else None

    def list_installed(self) -> list[SkillInfo]:
        skills_dir = cfg.user_skills_dir
        builtin_dir = cfg.builtin_skills_dir

        builtin_names: set[str] = set()
        if builtin_dir.is_dir():
            for bd in builtin_dir.iterdir():
                if bd.is_dir() and (bd / "SKILL.md").exists():
                    builtin_names.add(bd.name)

        plugin_skill_names: set[str] = set()
        try:
            from .plugins import get_plugin_registry

            for p in get_plugin_registry().list_plugins():
                for sn in p.get("skills", []):
                    plugin_skill_names.add(sn)
        except Exception:
            pass

        # Collect skills from both builtin and user dirs.
        # User-dir skills override builtins with the same directory name.
        seen: dict[str, SkillInfo] = {}

        # 1) Built-in skills
        if builtin_dir.is_dir():
            for d in sorted(builtin_dir.iterdir()):
                skill_file = d / "SKILL.md"
                if not d.is_dir() or not skill_file.exists():
                    continue
                fm = _parse_frontmatter(skill_file.read_text(errors="replace"))
                skill_name = fm.get("name", d.name)
                seen[d.name] = SkillInfo(
                    name=skill_name,
                    verb=fm.get("verb", d.name),
                    description=fm.get("description", ""),
                    source="local",
                    category="local",
                    installed=True,
                    recommended=skill_name in _CURATED_SKILLS,
                    origin="built-in",
                )

        # 2) User skills (may override builtins)
        if skills_dir.is_dir():
            for d in sorted(skills_dir.iterdir()):
                skill_file = d / "SKILL.md"
                if not d.is_dir() or not skill_file.exists():
                    continue
                fm = _parse_frontmatter(skill_file.read_text(errors="replace"))
                skill_name = fm.get("name", d.name)
                origin = _determine_origin(d, builtin_names, plugin_skill_names)
                seen[d.name] = SkillInfo(
                    name=skill_name,
                    verb=fm.get("verb", d.name),
                    description=fm.get("description", ""),
                    source="local",
                    category="local",
                    installed=True,
                    recommended=skill_name in _CURATED_SKILLS,
                    origin=origin,
                )

        return list(seen.values())

    def get_installed(self, name: str) -> SkillInfo | None:
        return next((s for s in self.list_installed() if s.name == name), None)

    def remove(self, name: str) -> bool:
        target = cfg.user_skills_dir / name
        if target.is_dir() and (target / "SKILL.md").exists():
            shutil.rmtree(target)
            logger.info("Removed skill: %s", name)
            return True
        return False

    async def fetch_catalog(self, *, force: bool = False) -> list[SkillInfo]:
        import time

        from .catalog import fetch_catalog as _fetch_catalog

        now = time.monotonic()
        if (
            not force
            and self._catalog_cache is not None
            and (now - self._catalog_ts) < 300
        ):
            return self._catalog_cache

        installed_names = {s.name for s in self.list_installed()}
        self.rate_limited = False
        self.rate_limit_reset = None

        all_skills, rate_limited, rate_limit_reset = await _fetch_catalog(
            installed_names, _parse_frontmatter, _CURATED_SKILLS,
        )
        self.rate_limited = rate_limited
        self.rate_limit_reset = rate_limit_reset

        self._catalog_cache = all_skills
        self._catalog_ts = now
        return all_skills

    async def install(self, name: str) -> str | None:
        from .catalog import install_from_catalog

        catalog = await self.fetch_catalog()
        skill = next((s for s in catalog if s.name == name), None)
        if not skill:
            return f"Skill {name!r} not found in catalog ({len(catalog)} skills available)"
        if skill.installed:
            return None

        target_dir = cfg.user_skills_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)

        error = await install_from_catalog(skill, target_dir)
        if error:
            return error

        self._catalog_cache = None
        logger.info("Installed skill: %s -> %s", name, target_dir)
        return None


_registry: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry


def set_registry(instance: SkillRegistry) -> None:
    global _registry
    _registry = instance


def _reset_registry() -> None:
    global _registry
    _registry = None


register_singleton(_reset_registry)
