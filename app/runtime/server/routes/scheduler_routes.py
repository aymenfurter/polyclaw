"""Scheduler CRUD API routes -- /api/schedules/*."""

from __future__ import annotations

from dataclasses import asdict

from aiohttp import web

from ...scheduler import Scheduler
from ._helpers import api_handler, error_response, ok_response, parse_json


class SchedulerRoutes:
    """REST handler for scheduled tasks."""

    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/schedules", self._list)
        router.add_post("/api/schedules", self._create)
        router.add_put("/api/schedules/{task_id}", self._update)
        router.add_delete("/api/schedules/{task_id}", self._delete)

    async def _list(self, _req: web.Request) -> web.Response:
        tasks = self._scheduler.list_tasks()
        return web.json_response([asdict(t) for t in tasks])

    @api_handler
    async def _create(self, req: web.Request) -> web.Response:
        data = await parse_json(req)
        task = self._scheduler.add(
            description=data.get("description") or data.get("name", ""),
            prompt=data.get("prompt", ""),
            cron=data.get("cron") or data.get("schedule"),
            run_at=data.get("run_at"),
        )
        return ok_response(task=asdict(task))

    @api_handler
    async def _update(self, req: web.Request) -> web.Response:
        task_id = req.match_info["task_id"]
        data = await parse_json(req)
        # Normalise frontend field aliases
        if "schedule" in data and "cron" not in data:
            data["cron"] = data.pop("schedule")
        if "name" in data and "description" not in data:
            data["description"] = data.pop("name")
        task = self._scheduler.update(task_id, **data)
        if not task:
            return error_response("Task not found", status=404)
        return ok_response(task=asdict(task))

    async def _delete(self, req: web.Request) -> web.Response:
        task_id = req.match_info["task_id"]
        removed = self._scheduler.remove(task_id)
        if not removed:
            return error_response("Task not found", status=404)
        return ok_response()
