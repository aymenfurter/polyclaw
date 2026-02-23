"""GitHub skill catalog -- fetch remote skill listings and install them."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

import aiohttp

from ..config.settings import cfg

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
_ORIGIN_FILE = ".origin"


def _github_headers() -> dict[str, str]:
    """Build common headers for GitHub API requests."""
    headers: dict[str, str] = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "polyclaw-skill-registry",
    }
    token = cfg.github_token
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


async def fetch_catalog(
    installed_names: set[str],
    parse_frontmatter: Any,
    curated_skills: set[str],
) -> tuple[list[Any], bool, int | None]:
    """Fetch remote skill catalog from all configured GitHub sources.

    Returns ``(skills, rate_limited, rate_limit_reset)``.
    """
    from .skills import SkillInfo

    rate_limited = False
    rate_limit_reset: int | None = None

    headers = _github_headers()
    all_skills: list[SkillInfo] = []

    async with aiohttp.ClientSession(headers=headers) as session:
        tasks = [
            _fetch_source(session, src, installed_names, parse_frontmatter, curated_skills)
            for src in _CATALOG_SOURCES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, res in enumerate(results):
        if isinstance(res, list):
            all_skills.extend(res)
        elif isinstance(res, _RateLimited):
            rate_limited = True
            rate_limit_reset = res.reset_at
        elif isinstance(res, Exception):
            logger.error("Catalog source %s failed: %s", _CATALOG_SOURCES[i]["label"], res)

    try:
        await _fetch_commit_counts(all_skills)
    except Exception:
        pass

    return all_skills, rate_limited, rate_limit_reset


class _RateLimited(Exception):
    """Raised internally when GitHub returns a 403 rate-limit response."""

    def __init__(self, reset_at: int | None = None) -> None:
        self.reset_at = reset_at


async def _fetch_source(
    session: aiohttp.ClientSession,
    src: dict[str, str],
    installed_names: set[str],
    parse_frontmatter: Any,
    curated_skills: set[str],
) -> list[Any]:
    from .skills import SkillInfo

    url = (
        f"{_GITHUB_API}/repos/{src['owner']}/{src['repo']}"
        f"/contents/{src['path']}?ref={src['branch']}"
    )
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                if resp.status == 403 and remaining == "0":
                    reset_at: int | None = None
                    try:
                        reset_at = int(resp.headers.get("X-RateLimit-Reset", "0"))
                    except (ValueError, TypeError):
                        pass
                    raise _RateLimited(reset_at)
                return []
            entries = await resp.json()
    except _RateLimited:
        raise
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
                    fm = parse_frontmatter(await r.text()) if r.status == 200 else {}
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
                recommended=skill_name in curated_skills,
            )

    results = await asyncio.gather(
        *[_get_skill(e["name"]) for e in entries if e.get("type") == "dir"],
        return_exceptions=True,
    )
    return [r for r in results if isinstance(r, SkillInfo)]


async def _fetch_commit_counts(skills: list[Any]) -> None:
    headers = _github_headers()
    sem = asyncio.Semaphore(10)

    async def _get_count(session: aiohttp.ClientSession, skill: Any) -> None:
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


async def install_from_catalog(
    skill: Any,
    target_dir: Path,
) -> str | None:
    """Download a skill from GitHub into *target_dir*.

    Returns ``None`` on success, or an error message string.
    """
    headers = _github_headers()

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            await _download_dir(
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
        return f"Download failed for skill {skill.name!r}: {exc}"

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
    return None


async def _download_dir(
    session: aiohttp.ClientSession,
    *,
    owner: str,
    repo: str,
    path: str,
    branch: str,
    target: Path,
) -> None:
    """Recursively download a directory from a GitHub repo."""
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
            await _download_dir(
                session,
                owner=owner,
                repo=repo,
                path=entry["path"],
                branch=branch,
                target=sub_dir,
            )
