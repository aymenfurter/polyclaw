"""Skill management API routes -- /api/skills/*."""

from __future__ import annotations

import logging
import time as _time

from aiohttp import web

from ...registries.skills import SkillRegistry
from ...state.profile import load_skill_usage

logger = logging.getLogger(__name__)


class SkillRoutes:
    """REST handler for skill management (install, catalog, marketplace)."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/skills", self._list)
        router.add_get("/api/skills/installed", self._installed)
        router.add_get("/api/skills/catalog", self._catalog)
        router.add_get("/api/skills/marketplace", self._marketplace)
        router.add_post("/api/skills/install", self._install)
        router.add_delete("/api/skills/{skill_id}", self._remove)
        router.add_post("/api/skills/contribute", self._contribute)

    async def _list(self, _req: web.Request) -> web.Response:
        return web.json_response({"skills": [s.to_dict() for s in self._registry.list_installed()]})

    async def _installed(self, _req: web.Request) -> web.Response:
        return web.json_response([s.to_dict() for s in self._registry.list_installed()])

    async def _catalog(self, _req: web.Request) -> web.Response:
        skills = await self._registry.fetch_catalog()
        return web.json_response([s.to_dict() for s in skills])

    async def _marketplace(self, req: web.Request) -> web.Response:
        force = req.query.get("refresh") == "1"
        try:
            catalog = await self._registry.fetch_catalog(force=force)
        except Exception as exc:
            logger.warning("Marketplace catalog fetch failed: %s", exc)
            catalog = []

        installed = self._registry.list_installed()
        usage = load_skill_usage()

        # Build unified skill map (installed + catalog)
        skill_map: dict[str, dict] = {}
        for s in installed:
            d = s.to_dict()
            d["usage_count"] = usage.get(s.name, 0)
            skill_map[s.name] = d
        for s in catalog:
            d = s.to_dict()
            d["usage_count"] = usage.get(s.name, 0)
            if s.name in skill_map:
                skill_map[s.name].update(
                    {
                        "source": d["source"],
                        "category": d["category"],
                        "edit_count": d["edit_count"],
                        "recommended": d["recommended"]
                        or skill_map[s.name].get("recommended", False),
                    }
                )
            else:
                skill_map[s.name] = d

        all_skills = list(skill_map.values())

        recommended = [s for s in all_skills if s.get("recommended")]

        by_edits = sorted(
            all_skills,
            key=lambda s: s.get("edit_count", 0),
            reverse=True,
        )
        popular = [s for s in by_edits if s.get("edit_count", 0) > 0][:8]

        loved_names = sorted(usage, key=usage.get, reverse=True)  # type: ignore[arg-type]
        loved = [skill_map[n] for n in loved_names if n in skill_map and usage[n] > 0][:6]

        github_awesome = [s for s in all_skills if s.get("category") == "github-awesome"]
        anthropic = [s for s in all_skills if s.get("category") == "anthropic"]
        installed_list = [s for s in all_skills if s.get("installed")]

        result: dict = {
            "recommended": recommended,
            "popular": popular,
            "loved": loved,
            "github_awesome": github_awesome,
            "anthropic": anthropic,
            "installed": installed_list,
            "all": all_skills,
        }

        if self._registry.rate_limited:
            reset_ts = self._registry.rate_limit_reset
            if reset_ts:
                wait_min = max(1, int((reset_ts - _time.time()) / 60))
                result["rate_limit_warning"] = (
                    f"GitHub API rate limit exceeded for your IP. "
                    f"Catalog results may be incomplete. "
                    f"Rate limit resets in ~{wait_min} min. "
                    f"Set a GITHUB_TOKEN for a higher limit."
                )
            else:
                result["rate_limit_warning"] = (
                    "GitHub API rate limit exceeded for your IP. "
                    "Catalog results may be incomplete. "
                    "Set a GITHUB_TOKEN for a higher limit."
                )

        return web.json_response(result)

    async def _install(self, req: web.Request) -> web.Response:
        body = await req.json()
        name = body.get("url", "").strip() or body.get("name", "").strip()
        if not name:
            return web.json_response(
                {"status": "error", "message": "url or name is required"}, status=400
            )
        try:
            error = await self._registry.install(name)
            if error:
                return web.json_response(
                    {"status": "error", "message": error}, status=400
                )
            return web.json_response({"status": "ok"})
        except Exception as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )

    async def _remove(self, req: web.Request) -> web.Response:
        skill_id = req.match_info["skill_id"]
        removed = self._registry.remove(skill_id)
        if not removed:
            return web.json_response(
                {"status": "error", "message": "Skill not found"}, status=404
            )
        return web.json_response({"status": "ok"})

    async def _contribute(self, req: web.Request) -> web.Response:
        body = await req.json()
        skill_id = body.get("skill_id", "").strip()
        if not skill_id:
            return web.json_response(
                {"status": "error", "message": "skill_id is required"}, status=400
            )
        content = self._registry.get_skill_content(skill_id)
        if content is None:
            return web.json_response(
                {"status": "error", "message": f"Skill '{skill_id}' not found"}, status=400
            )
        skill_path = f"skills/{skill_id}/SKILL.md"
        github_url = (
            "https://github.com/aymenfurter/octoclaw/new/main?"
            f"filename={skill_path}"
        )
        return web.json_response({
            "status": "ok",
            "skill_id": skill_id,
            "skill_name": skill_id,
            "files": [{
                "path": skill_path,
                "content": content,
                "github_url": github_url,
            }],
        })
