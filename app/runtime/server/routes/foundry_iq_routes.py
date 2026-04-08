"""Foundry IQ admin routes -- /api/foundry-iq/*."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from ...services.cloud.azure import AzureCLI
from ...services.deployment.bicep_deployer import BicepDeployer, BicepDeployRequest
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
from ._helpers import api_handler, error_response, ok_response, parse_json
from ._helpers import fail_response as _fail_response
from ._helpers import no_az as _no_az

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
        self._bicep = BicepDeployer(az, deploy_store) if az and deploy_store else None

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

    @api_handler
    async def _save_config(self, req: web.Request) -> web.Response:
        data = await parse_json(req)
        self._store.save(**data)
        return ok_response(config=self._store.to_safe_dict())

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
            return error_response(f"Indexing crashed: {exc}", status=500)
        return web.json_response(result)

    async def _get_stats(self, _req: web.Request) -> web.Response:
        result = await run_sync(get_index_stats, self._store)
        return web.json_response(result)

    @api_handler
    async def _search(self, req: web.Request) -> web.Response:
        data = await parse_json(req)
        query = data.get("query", "").strip()
        if not query:
            return error_response("Query is required")
        top = data.get("top", 5)
        result = await run_sync(search_memories, query, top, self._store)
        return web.json_response(result)

    async def _provision(self, req: web.Request) -> web.Response:
        if not self._bicep:
            return _no_az()
        if self._store.is_provisioned:
            return ok_response(
                message="Already provisioned",
                steps=[],
                config=self._store.to_safe_dict(),
            )

        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:
            body = {}

        location = body.get("location", "eastus").strip()
        rg = body.get("resource_group", "").strip() or _DEFAULT_FIQ_RG
        embedding_model = body.get("embedding_model", "text-embedding-3-large").strip()
        embedding_dimensions = int(body.get("embedding_dimensions", 3072))

        bicep_req = BicepDeployRequest(
            resource_group=rg,
            location=location,
            deploy_foundry=False,
            deploy_key_vault=False,
            deploy_search=True,
            deploy_embedding_aoai=True,
            embedding_model_name=embedding_model,
        )
        result = await run_sync(self._bicep.deploy, bicep_req)

        if not result.ok:
            return _fail_response(result.steps)

        # Retrieve the search admin key (not available as a Bicep output)
        search_key = ""
        if result.search_name and self._az:
            keys = await run_sync(
                self._az.json,
                "search", "admin-key", "show",
                "--service-name", result.search_name,
                "--resource-group", rg,
            )
            search_key = keys.get("primaryKey", "") if isinstance(keys, dict) else ""
            result.steps.append({
                "step": "search_key",
                "status": "ok" if search_key else "warning",
                "detail": "Key retrieved" if search_key else "Key unavailable",
            })

        # Retrieve the AOAI key (fallback; prefer Entra ID)
        aoai_key = ""
        if result.embedding_aoai_name and self._az:
            aoai_keys = await run_sync(
                self._az.json,
                "cognitiveservices", "account", "keys", "list",
                "--name", result.embedding_aoai_name,
                "--resource-group", rg,
            )
            aoai_key = aoai_keys.get("key1", "") if isinstance(aoai_keys, dict) else ""

        self._store.save(
            resource_group=rg,
            location=location,
            search_resource_name=result.search_name,
            openai_resource_name=result.embedding_aoai_name,
            openai_deployment_name=result.embedding_deployment_name,
            search_endpoint=result.search_endpoint,
            search_api_key=search_key,
            embedding_endpoint=result.embedding_aoai_endpoint,
            embedding_api_key=aoai_key,
            embedding_model=result.embedding_deployment_name,
            embedding_dimensions=embedding_dimensions,
            index_name="polyclaw-memories",
            provisioned=True,
            enabled=True,
        )
        result.steps.append({"step": "save_config", "status": "ok", "detail": "Saved"})

        try:
            idx_result = await run_sync(ensure_index, self._store)
            idx_ok = idx_result.get("status") == "ok"
            result.steps.append({
                "step": "create_index",
                "status": "ok" if idx_ok else "failed",
                "detail": idx_result.get("detail", ""),
            })
        except Exception as exc:
            result.steps.append({
                "step": "create_index", "status": "failed", "detail": str(exc)[:200]
            })

        logger.info(
            "Foundry IQ provisioned (Bicep): search=%s, aoai=%s",
            result.search_name, result.embedding_aoai_name,
        )
        return ok_response(
            message=f"Foundry IQ provisioned in {rg}",
            steps=result.steps,
            config=self._store.to_safe_dict(),
        )

    async def _decommission(self, _req: web.Request) -> web.Response:
        if not self._az:
            return _no_az()
        if not self._store.is_provisioned:
            return error_response("Nothing provisioned")

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
        return ok_response(message="Resources removed", steps=steps)
