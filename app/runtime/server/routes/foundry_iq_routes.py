"""Foundry IQ admin routes -- /api/foundry-iq/*."""

from __future__ import annotations

import logging
import secrets as _secrets
from typing import Any

from aiohttp import web

from ...services.cloud.azure import AzureCLI
from ...services.foundry_iq import (
    delete_index,
    ensure_index,
    get_index_stats,
    index_memories,
    search_memories,
    test_embedding_connection,
    test_search_connection,
)
from ...state.deploy_state import DeployStateStore
from ...state.foundry_iq_config import FoundryIQConfigStore
from ...util.async_helpers import run_sync
from ._helpers import fail_response as _fail_response, no_az as _no_az

logger = logging.getLogger(__name__)

_DEFAULT_FIQ_RG = "polyclaw-foundryiq-rg"


class FoundryIQRoutes:
    """REST handler for Foundry IQ configuration and operations."""

    def __init__(
        self,
        config_store: FoundryIQConfigStore,
        az: AzureCLI | None = None,
        deploy_store: DeployStateStore | None = None,
    ) -> None:
        self._store = config_store
        self._az = az
        self._deploy_store = deploy_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/foundry-iq/config", self._get_config)
        router.add_put("/api/foundry-iq/config", self._save_config)
        router.add_post("/api/foundry-iq/test-search", self._test_search)
        router.add_post("/api/foundry-iq/test-embedding", self._test_embedding)
        router.add_post("/api/foundry-iq/ensure-index", self._ensure_index)
        router.add_delete("/api/foundry-iq/index", self._delete_index)
        router.add_post("/api/foundry-iq/index", self._run_indexing)
        router.add_get("/api/foundry-iq/stats", self._get_stats)
        router.add_post("/api/foundry-iq/search", self._search)
        router.add_post("/api/foundry-iq/provision", self._provision)
        router.add_delete("/api/foundry-iq/provision", self._decommission)

    async def _get_config(self, _req: web.Request) -> web.Response:
        return web.json_response(self._store.to_safe_dict())

    async def _save_config(self, req: web.Request) -> web.Response:
        data = await req.json()
        self._store.save(**data)
        return web.json_response({"status": "ok", "config": self._store.to_safe_dict()})

    async def _test_search(self, _req: web.Request) -> web.Response:
        result = await run_sync(test_search_connection, self._store)
        return web.json_response(result)

    async def _test_embedding(self, _req: web.Request) -> web.Response:
        result = await run_sync(test_embedding_connection, self._store)
        return web.json_response(result)

    async def _ensure_index(self, _req: web.Request) -> web.Response:
        result = await run_sync(ensure_index, self._store)
        return web.json_response(result)

    async def _delete_index(self, _req: web.Request) -> web.Response:
        result = await run_sync(delete_index, self._store)
        return web.json_response(result)

    async def _run_indexing(self, _req: web.Request) -> web.Response:
        try:
            result = await run_sync(index_memories, self._store)
        except Exception as exc:
            logger.exception("Indexing failed")
            return web.json_response(
                {"status": "error", "message": f"Indexing crashed: {exc}"},
                status=500,
            )
        return web.json_response(result)

    async def _get_stats(self, _req: web.Request) -> web.Response:
        result = await run_sync(get_index_stats, self._store)
        return web.json_response(result)

    async def _search(self, req: web.Request) -> web.Response:
        data = await req.json()
        query = data.get("query", "").strip()
        if not query:
            return web.json_response(
                {"status": "error", "message": "Query is required"}, status=400
            )
        top = data.get("top", 5)
        result = await run_sync(search_memories, query, top, self._store)
        return web.json_response(result)

    async def _provision(self, req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        if self._store.is_provisioned:
            return web.json_response({
                "status": "ok",
                "message": "Already provisioned",
                "steps": [],
                "config": self._store.to_safe_dict(),
            })

        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:
            body = {}

        location = body.get("location", "eastus").strip()
        rg = body.get("resource_group", "").strip() or _DEFAULT_FIQ_RG
        search_name = (
            body.get("search_name", "").strip()
            or f"polyclaw-search-{_secrets.token_hex(4)}"
        )
        openai_name = (
            body.get("openai_name", "").strip()
            or f"polyclaw-aoai-{_secrets.token_hex(4)}"
        )
        embedding_model = body.get("embedding_model", "text-embedding-3-large").strip()
        embedding_dimensions = int(body.get("embedding_dimensions", 3072))

        steps: list[dict[str, Any]] = []

        if not await self._ensure_rg(rg, location, steps):
            return _fail_response(steps)

        search_result = await self._create_search(rg, location, search_name, steps)
        if not search_result:
            return _fail_response(steps)
        search_endpoint, search_key = search_result

        openai_result = await self._create_openai(rg, location, openai_name, steps)
        if not openai_result:
            return _fail_response(steps)
        openai_endpoint, openai_key = openai_result

        deployment_name = await self._deploy_model(
            rg, openai_name, embedding_model, steps
        )
        if not deployment_name:
            return _fail_response(steps)

        self._store.save(
            resource_group=rg,
            location=location,
            search_resource_name=search_name,
            openai_resource_name=openai_name,
            openai_deployment_name=deployment_name,
            search_endpoint=search_endpoint,
            search_api_key=search_key,
            embedding_endpoint=openai_endpoint,
            embedding_api_key=openai_key,
            embedding_model=deployment_name,
            embedding_dimensions=embedding_dimensions,
            index_name="polyclaw-memories",
            provisioned=True,
            enabled=True,
        )
        steps.append({"step": "save_config", "status": "ok", "detail": "Saved"})

        try:
            idx_result = await run_sync(ensure_index, self._store)
            idx_ok = idx_result.get("status") == "ok"
            steps.append({
                "step": "create_index",
                "status": "ok" if idx_ok else "failed",
                "detail": idx_result.get("detail", ""),
            })
        except Exception as exc:
            steps.append({
                "step": "create_index", "status": "failed", "detail": str(exc)[:200]
            })

        logger.info("Foundry IQ provisioned: search=%s, openai=%s", search_name, openai_name)
        return web.json_response({
            "status": "ok",
            "message": f"Foundry IQ provisioned in {rg}",
            "steps": steps,
            "config": self._store.to_safe_dict(),
        })

    async def _decommission(self, _req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        if not self._store.is_provisioned:
            return web.json_response(
                {"status": "error", "message": "Nothing provisioned"}, status=400
            )

        steps: list[dict[str, Any]] = []
        rg = self._store.config.resource_group
        search_name = self._store.config.search_resource_name
        openai_name = self._store.config.openai_resource_name

        try:
            await run_sync(delete_index, self._store)
            steps.append({"step": "delete_index", "status": "ok", "detail": "Deleted"})
        except Exception:
            steps.append({"step": "delete_index", "status": "skip", "detail": "N/A"})

        if search_name and rg:
            ok, msg = await run_sync(
                self._az.ok,
                "search", "service", "delete",
                "--name", search_name, "--resource-group", rg, "--yes",
            )
            steps.append({
                "step": "delete_search",
                "status": "ok" if ok else "failed",
                "detail": f"Deleted {search_name}" if ok else (msg or "Unknown"),
            })

        if openai_name and rg:
            ok, msg = await run_sync(
                self._az.ok,
                "cognitiveservices", "account", "delete",
                "--name", openai_name, "--resource-group", rg,
            )
            steps.append({
                "step": "delete_openai",
                "status": "ok" if ok else "failed",
                "detail": f"Deleted {openai_name}" if ok else (msg or "Unknown"),
            })

        if rg == _DEFAULT_FIQ_RG:
            rg_ok, rg_msg = await run_sync(
                self._az.ok, "group", "delete", "--name", rg, "--yes", "--no-wait",
            )
            steps.append({
                "step": "delete_rg",
                "status": "ok" if rg_ok else "failed",
                "detail": f"Deleting {rg}" if rg_ok else (rg_msg or "Unknown"),
            })

        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.resources = [
                    r for r in rec.resources
                    if r.resource_name not in (search_name, openai_name)
                ]
                if rg in rec.resource_groups:
                    rec.resource_groups.remove(rg)
                self._deploy_store.update(rec)

        self._store.clear_provisioning()
        steps.append({"step": "clear_config", "status": "ok", "detail": "Cleared"})

        logger.info("Foundry IQ decommissioned: %s, %s", search_name, openai_name)
        return web.json_response({
            "status": "ok", "message": "Resources removed", "steps": steps
        })

    # -- internal helpers --

    async def _ensure_rg(
        self, rg: str, location: str, steps: list[dict[str, Any]]
    ) -> bool:
        existing = await run_sync(self._az.json, "group", "show", "--name", rg)
        if existing:
            steps.append({
                "step": "resource_group", "status": "ok", "detail": f"{rg} (existing)"
            })
            return True

        tag_args: list[str] = []
        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                tag_args = ["--tags", f"polyclaw_deploy={rec.tag}"]

        result = await run_sync(
            self._az.json,
            "group", "create", "--name", rg, "--location", location, *tag_args,
        )
        ok = bool(result)
        steps.append({
            "step": "resource_group",
            "status": "ok" if ok else "failed",
            "detail": rg if ok else (self._az.last_stderr or "Unknown error"),
        })
        if ok and self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec and rg not in rec.resource_groups:
                rec.resource_groups.append(rg)
                self._deploy_store.update(rec)
        return ok

    async def _create_search(
        self, rg: str, location: str, name: str, steps: list[dict[str, Any]]
    ) -> tuple[str, str] | None:
        result = await run_sync(
            self._az.json,
            "search", "service", "create",
            "--name", name, "--resource-group", rg,
            "--location", location, "--sku", "basic",
            "--partition-count", "1", "--replica-count", "1",
        )
        if not result or not isinstance(result, dict):
            steps.append({
                "step": "create_search", "status": "failed",
                "detail": (self._az.last_stderr or "Unknown")[:300],
            })
            return None

        host_name = result.get("hostName") or f"{name}.search.windows.net"
        endpoint = f"https://{host_name}"
        steps.append({
            "step": "create_search", "status": "ok", "detail": f"{name} ({endpoint})"
        })

        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.add_resource(
                    resource_type="search", resource_group=rg,
                    resource_name=name, purpose="Foundry IQ - Azure AI Search",
                    resource_id=result.get("id", ""),
                )
                self._deploy_store.update(rec)

        keys = await run_sync(
            self._az.json,
            "search", "admin-key", "show",
            "--service-name", name, "--resource-group", rg,
        )
        admin_key = keys.get("primaryKey", "") if isinstance(keys, dict) else ""
        if not admin_key:
            steps.append({
                "step": "search_key", "status": "failed",
                "detail": (self._az.last_stderr or "Key empty")[:300],
            })
            return None
        steps.append({"step": "search_key", "status": "ok", "detail": "Key retrieved"})
        return endpoint, admin_key

    async def _create_openai(
        self, rg: str, location: str, name: str, steps: list[dict[str, Any]]
    ) -> tuple[str, str] | None:
        result = await run_sync(
            self._az.json,
            "cognitiveservices", "account", "create",
            "--name", name, "--resource-group", rg,
            "--location", location, "--kind", "OpenAI",
            "--sku", "S0", "--custom-domain", name,
        )
        if not result or not isinstance(result, dict):
            steps.append({
                "step": "create_openai", "status": "failed",
                "detail": (self._az.last_stderr or "Unknown")[:300],
            })
            return None

        steps.append({
            "step": "create_openai", "status": "ok", "detail": f"{name} created"
        })

        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.add_resource(
                    resource_type="cognitiveservices", resource_group=rg,
                    resource_name=name, purpose="Foundry IQ - Azure OpenAI",
                    resource_id=result.get("id", ""),
                )
                self._deploy_store.update(rec)

        info = await run_sync(
            self._az.json,
            "cognitiveservices", "account", "show",
            "--name", name, "--resource-group", rg,
        )
        endpoint = ""
        if isinstance(info, dict):
            endpoint = info.get("properties", {}).get("endpoint", "")
        if not endpoint:
            endpoint = f"https://{name}.openai.azure.com/"

        aoai_keys = await run_sync(
            self._az.json,
            "cognitiveservices", "account", "keys", "list",
            "--name", name, "--resource-group", rg,
        )
        api_key = aoai_keys.get("key1", "") if isinstance(aoai_keys, dict) else ""
        if api_key:
            steps.append({"step": "openai_key", "status": "ok", "detail": "Key retrieved"})
        else:
            steps.append({
                "step": "openai_key", "status": "ok",
                "detail": "Key-based auth disabled; will use Entra ID",
            })
        return endpoint, api_key

    async def _deploy_model(
        self, rg: str, account: str, model: str, steps: list[dict[str, Any]]
    ) -> str | None:
        deployment_name = model
        result = await run_sync(
            self._az.json,
            "cognitiveservices", "account", "deployment", "create",
            "--name", account, "--resource-group", rg,
            "--deployment-name", deployment_name,
            "--model-name", model, "--model-version", "1",
            "--model-format", "OpenAI",
            "--sku-capacity", "1", "--sku-name", "Standard",
        )
        if result is None:
            err = self._az.last_stderr or ""
            if "already exists" in err.lower() or "conflict" in err.lower():
                steps.append({
                    "step": "deploy_model", "status": "ok",
                    "detail": f"{deployment_name} (already exists)",
                })
                return deployment_name
            steps.append({
                "step": "deploy_model", "status": "failed", "detail": err[:300]
            })
            return None

        steps.append({
            "step": "deploy_model", "status": "ok",
            "detail": f"{deployment_name} deployed",
        })
        return deployment_name

