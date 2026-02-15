"""Skill registry -- list, install, and remove agent skills."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from ..config.settings import cfg
from ..util.singletons import register_singleton

logger = logging.getLogger(__name__)

_CATALOG_SOURCES: list[dict[str, str]] = [
    {
        "owner": "github",
        "repo": "awesome-copilot",
        "path": "skills",
        "branch": "main",
        "label": "GitHub Awesome Copilot",
        "category": "github-awesome",
    },
    {
        "owner": "anthropics",
        "repo": "skills",
        "path": "skills",
        "branch": "main",
        "label": "Anthropic Skills",
        "category": "anthropic",
    },
]

_GITHUB_API = "https://api.github.com"
_GITHUB_RAW = "https://raw.githubusercontent.com"
_CATALOG_CACHE_TTL = 300
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

        now = time.monotonic()
        if (
            not force
            and self._catalog_cache is not None
            and (now - self._catalog_ts) < _CATALOG_CACHE_TTL
        ):
            return self._catalog_cache

        installed_names = {s.name for s in self.list_installed()}
        self.rate_limited = False
        self.rate_limit_reset = None

        headers: dict[str, str] = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "octoclaw-skill-registry",
        }
        token = cfg.github_token
        if token:
            headers["Authorization"] = f"token {token}"

        all_skills: list[SkillInfo] = []
        async with aiohttp.ClientSession(headers=headers) as session:
            tasks = [self._fetch_source(session, src, installed_names) for src in _CATALOG_SOURCES]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, res in enumerate(results):
            if isinstance(res, list):
                all_skills.extend(res)
            elif isinstance(res, Exception):
                logger.error("Catalog source %s failed: %s", _CATALOG_SOURCES[i]["label"], res)

        try:
            await self._fetch_commit_counts(all_skills)
        except Exception:
            pass

        self._catalog_cache = all_skills
        self._catalog_ts = now
        return all_skills

    async def _fetch_source(
        self,
        session: aiohttp.ClientSession,
        src: dict[str, str],
        installed_names: set[str],
    ) -> list[SkillInfo]:
        url = (
            f"{_GITHUB_API}/repos/{src['owner']}/{src['repo']}"
            f"/contents/{src['path']}?ref={src['branch']}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                    if resp.status == 403 and remaining == "0":
                        self.rate_limited = True
                        try:
                            self.rate_limit_reset = int(
                                resp.headers.get("X-RateLimit-Reset", "0")
                            )
                        except (ValueError, TypeError):
                            pass
                    return []
                entries = await resp.json()
        except Exception as exc:
            logger.error("GitHub API request failed: %s", exc)
            return []

        if not isinstance(entries, list):
            return []

        sem = asyncio.Semaphore(20)

        async def _get_skill(name: str) -> SkillInfo | None:
            async with sem:
                raw_url = (
                    f"{_GITHUB_RAW}/{src['owner']}/{src['repo']}"
                    f"/{src['branch']}/{src['path']}/{name}/SKILL.md"
                )
                try:
                    async with session.get(raw_url) as r:
                        fm = _parse_frontmatter(await r.text()) if r.status == 200 else {}
                except Exception:
                    fm = {}
                skill_name = fm.get("name", name)
                return SkillInfo(
                    name=skill_name,
                    verb=fm.get("verb", name),
                    description=fm.get("description", ""),
                    source=src["label"],
                    category=src.get("category", ""),
                    repo_owner=src["owner"],
                    repo_name=src["repo"],
                    repo_path=f"{src['path']}/{name}",
                    repo_branch=src["branch"],
                    installed=skill_name in installed_names,
                    recommended=skill_name in _CURATED_SKILLS,
                )

        results = await asyncio.gather(
            *[_get_skill(e["name"]) for e in entries if e.get("type") == "dir"],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, SkillInfo)]

    async def _fetch_commit_counts(self, skills: list[SkillInfo]) -> None:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "octoclaw-skill-registry",
        }
        token = cfg.github_token
        if token:
            headers["Authorization"] = f"token {token}"
        sem = asyncio.Semaphore(10)

        async def _get_count(session: aiohttp.ClientSession, skill: SkillInfo) -> None:
            if not skill.repo_owner:
                return
            async with sem:
                url = (
                    f"{_GITHUB_API}/repos/{skill.repo_owner}/{skill.repo_name}"
                    f"/commits?path={skill.repo_path}&sha={skill.repo_branch}&per_page=1"
                )
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            return
                        link = resp.headers.get("Link", "")
                        match = re.search(r'page=(\d+)>; rel="last"', link)
                        if match:
                            skill.edit_count = int(match.group(1))
                        else:
                            data = await resp.json()
                            skill.edit_count = len(data) if isinstance(data, list) else 0
                except Exception:
                    pass

        async with aiohttp.ClientSession(headers=headers) as session:
            await asyncio.gather(
                *[_get_count(session, s) for s in skills], return_exceptions=True
            )

    async def install(self, name: str) -> str | None:
        catalog = await self.fetch_catalog()
        skill = next((s for s in catalog if s.name == name), None)
        if not skill:
            return f"Skill {name!r} not found in catalog ({len(catalog)} skills available)"
        if skill.installed:
            return None

        target_dir = cfg.user_skills_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)

        headers: dict[str, str] = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "octoclaw-skill-registry",
        }
        token = cfg.github_token
        if token:
            headers["Authorization"] = f"token {token}"

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                await self._download_dir(
                    session,
                    owner=skill.repo_owner,
                    repo=skill.repo_name,
                    path=skill.repo_path,
                    branch=skill.repo_branch,
                    target=target_dir,
                )
        except Exception as exc:
            if target_dir.exists():
                shutil.rmtree(target_dir)
            return f"Download failed for skill {name!r}: {exc}"

        origin_path = target_dir / _ORIGIN_FILE
        origin_path.write_text(
            json.dumps(
                {
                    "origin": "marketplace",
                    "source": skill.source,
                    "category": skill.category,
                    "repo_owner": skill.repo_owner,
                    "repo_name": skill.repo_name,
                    "repo_path": skill.repo_path,
                },
                indent=2,
            )
            + "\n"
        )

        self._catalog_cache = None
        logger.info("Installed skill: %s -> %s", name, target_dir)
        return None

    async def _download_dir(
        self,
        session: aiohttp.ClientSession,
        *,
        owner: str,
        repo: str,
        path: str,
        branch: str,
        target: Path,
    ) -> None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={branch}"
        async with session.get(url) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"GitHub API HTTP {resp.status} for {url}: {body[:500]}")
            entries = await resp.json()

        if not isinstance(entries, list):
            entries = [entries]

        for entry in entries:
            if entry["type"] == "file":
                raw_url = (
                    entry.get("download_url")
                    or f"{_GITHUB_RAW}/{owner}/{repo}/{branch}/{entry['path']}"
                )
                async with session.get(raw_url) as file_resp:
                    if file_resp.status == 200:
                        (target / entry["name"]).write_bytes(await file_resp.read())
            elif entry["type"] == "dir":
                sub_dir = target / entry["name"]
                sub_dir.mkdir(parents=True, exist_ok=True)
                await self._download_dir(
                    session,
                    owner=owner,
                    repo=repo,
                    path=entry["path"],
                    branch=branch,
                    target=sub_dir,
                )


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
