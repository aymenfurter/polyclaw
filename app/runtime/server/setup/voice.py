"""Voice setup routes -- ``/api/setup/voice/*``."""

from __future__ import annotations

import logging

from aiohttp import web

from ...config.settings import cfg
from ...services.cloud.azure import AzureCLI
from ...state.infra_config import InfraConfigStore
from ...util.async_helpers import run_sync
from .voice_provision import (
    create_acs,
    create_aoai,
    ensure_rbac,
    ensure_rg,
    persist_config,
)
from ._helpers import error_response as _error, ok_response as _ok

logger = logging.getLogger(__name__)


class VoiceSetupRoutes:
    """ACS + Azure OpenAI provisioning, phone config, and decommissioning."""

    def __init__(self, az: AzureCLI, store: InfraConfigStore) -> None:
        self._az = az
        self._store = store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/setup/voice/config", self.get_config)
        router.add_post("/api/setup/voice/deploy", self.deploy)
        router.add_post("/api/setup/voice/connect", self.connect_existing)
        router.add_post("/api/setup/voice/phone", self.save_phone)
        router.add_post("/api/setup/voice/decommission", self.decommission)
        router.add_get("/api/setup/voice/aoai/list", self.list_aoai)
        router.add_get("/api/setup/voice/aoai/deployments", self.list_aoai_deployments)
        router.add_post("/api/setup/voice/aoai/validate", self.validate_aoai)
        router.add_get("/api/setup/voice/acs/list", self.list_acs)
        router.add_get("/api/setup/voice/acs/phones", self.list_acs_phones)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    async def get_config(self, _req: web.Request) -> web.Response:
        vc = self._store.to_safe_dict().get("channels", {}).get("voice_call", {})
        if vc.get("acs_resource_name"):
            rg = vc.get("voice_resource_group") or vc.get("resource_group")
            if rg:
                account = self._az.account_info()
                sub_id = account.get("id", "") if account else ""
                if sub_id:
                    vc["portal_phone_url"] = (
                        f"https://portal.azure.com/#@/resource/subscriptions/{sub_id}"
                        f"/resourceGroups/{rg}"
                        f"/providers/Microsoft.Communication"
                        f"/CommunicationServices/{vc['acs_resource_name']}"
                        f"/phonenumbers"
                    )
        return web.json_response(vc)

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    async def deploy(self, req: web.Request) -> web.Response:
        body = await req.json()
        location = body.get("location", "swedencentral").strip()
        voice_rg = body.get("voice_resource_group", "").strip() or "polyclaw-voice-rg"
        logger.info("Voice deploy started: voice_rg=%s, location=%s", voice_rg, location)

        steps: list[dict] = []

        if not await ensure_rg(self._az, voice_rg, location, steps):
            return _voice_fail(steps)

        acs_name, conn_str = await create_acs(self._az, voice_rg, steps)
        if not conn_str:
            return _voice_fail(steps)

        aoai_name, aoai_endpoint, aoai_key, deployment_name = await create_aoai(
            self._az, voice_rg, location, steps
        )
        if not aoai_endpoint:
            return _voice_fail(steps)

        if not aoai_key:
            await ensure_rbac(self._az, aoai_name, voice_rg, steps)

        persist_config(
            self._store, voice_rg, location, acs_name, conn_str,
            aoai_name, aoai_endpoint, aoai_key, deployment_name, steps,
        )
        logger.info("Voice deploy completed: acs=%s, aoai=%s", acs_name, aoai_name)

        reinit = req.app.get("_reinit_voice")
        if reinit:
            reinit()

        return web.json_response({
            "status": "ok",
            "steps": steps,
            "message": (
                "Voice infrastructure deployed."
                " Now purchase a phone number in the Azure Portal."
            ),
        })

    # ------------------------------------------------------------------
    # Phone
    # ------------------------------------------------------------------

    async def save_phone(self, req: web.Request) -> web.Response:
        body = await req.json()
        phone = body.get("phone_number", "").strip()
        target = body.get("target_number", "").strip()

        updates: dict[str, str] = {}
        env_updates: dict[str, str] = {}

        if phone:
            if not phone.startswith("+"):
                return _error("Source phone number must be in E.164 format (e.g. +14155551234)", 400)
            updates["acs_source_number"] = phone
            env_updates["ACS_SOURCE_NUMBER"] = phone

        if target:
            if not target.startswith("+"):
                return _error("Target phone number must be in E.164 format (e.g. +41781234567)", 400)
            updates["voice_target_number"] = target
            env_updates["VOICE_TARGET_NUMBER"] = target

        if not updates:
            return _error("At least one phone number is required", 400)

        self._store.save_voice_call(**updates)
        cfg.write_env(**env_updates)

        reinit = req.app.get("_reinit_voice")
        if reinit:
            reinit()

        return _ok("Phone number(s) saved")

    # ------------------------------------------------------------------
    # Decommission
    # ------------------------------------------------------------------

    async def decommission(self, req: web.Request) -> web.Response:
        vc = self._store.channels.voice_call
        voice_rg = vc.voice_resource_group or vc.resource_group
        steps: list[dict] = []

        if voice_rg:
            rg_exists = await run_sync(self._az.json, "group", "show", "--name", voice_rg)
            if rg_exists:
                ok, msg = await run_sync(
                    self._az.ok, "group", "delete", "--name", voice_rg, "--yes", "--no-wait",
                )
                steps.append({
                    "step": "voice_rg_delete",
                    "status": "ok" if ok else "failed",
                    "name": voice_rg,
                    "detail": f"Deleting {voice_rg}" if ok else msg,
                })
            else:
                steps.append({"step": "voice_rg_delete", "status": "skip", "detail": "RG not found"})
        else:
            rg = vc.resource_group
            if vc.acs_resource_name and rg:
                ok, _ = await run_sync(
                    self._az.ok, "communication", "delete",
                    "--name", vc.acs_resource_name, "--resource-group", rg, "--yes",
                )
                steps.append({
                    "step": "acs_resource",
                    "status": "ok" if ok else "failed",
                    "name": vc.acs_resource_name,
                })

            if vc.azure_openai_resource_name and rg:
                ok, _ = await run_sync(
                    self._az.ok, "cognitiveservices", "account", "delete",
                    "--name", vc.azure_openai_resource_name, "--resource-group", rg, "--yes",
                )
                steps.append({
                    "step": "aoai_resource",
                    "status": "ok" if ok else "failed",
                    "name": vc.azure_openai_resource_name,
                })

        self._store.clear_voice_call()
        cfg.write_env(
            ACS_CONNECTION_STRING="",
            ACS_SOURCE_NUMBER="",
            VOICE_TARGET_NUMBER="",
            AZURE_OPENAI_ENDPOINT="",
            AZURE_OPENAI_API_KEY="",
            AZURE_OPENAI_REALTIME_DEPLOYMENT="",
            ACS_CALLBACK_TOKEN="",
        )

        return web.json_response({
            "status": "ok",
            "steps": steps,
            "message": "Voice infrastructure decommissioned",
        })

    # ------------------------------------------------------------------
    # Discovery: AOAI
    # ------------------------------------------------------------------

    async def list_aoai(self, _req: web.Request) -> web.Response:
        resources = await run_sync(
            self._az.json, "resource", "list",
            "--resource-type", "Microsoft.CognitiveServices/accounts",
        )
        if not isinstance(resources, list):
            return web.json_response([])

        return web.json_response([
            {
                "name": r.get("name", ""),
                "resource_group": r.get("resourceGroup", ""),
                "location": r.get("location", ""),
            }
            for r in resources
            if r.get("kind") == "OpenAI"
        ])

    async def list_aoai_deployments(self, req: web.Request) -> web.Response:
        name = req.query.get("name", "").strip()
        rg = req.query.get("resource_group", "").strip()
        if not name or not rg:
            return _error("name and resource_group are required", 400)

        deployments = await run_sync(
            self._az.json, "cognitiveservices", "account", "deployment", "list",
            "--name", name, "--resource-group", rg,
        )
        if not isinstance(deployments, list):
            return web.json_response([])

        return web.json_response([
            {
                "deployment_name": d.get("name", ""),
                "model_name": d.get("properties", {}).get("model", {}).get("name", ""),
                "model_version": d.get("properties", {}).get("model", {}).get("version", ""),
                "model_format": d.get("properties", {}).get("model", {}).get("format", ""),
            }
            for d in deployments
        ])

    async def validate_aoai(self, req: web.Request) -> web.Response:
        body = await req.json()
        name = body.get("name", "").strip()
        rg = body.get("resource_group", "").strip()
        if not name or not rg:
            return _error("name and resource_group are required", 400)

        deployments = await run_sync(
            self._az.json, "cognitiveservices", "account", "deployment", "list",
            "--name", name, "--resource-group", rg,
        )
        if not isinstance(deployments, list):
            return web.json_response({
                "valid": False,
                "message": f"Cannot list deployments for {name}",
                "deployments": [],
            })

        realtime_models = {
            "gpt-4o-realtime-preview",
            "gpt-realtime-mini",
            "gpt-4o-mini-realtime-preview",
        }
        found = []
        for d in deployments:
            model = d.get("properties", {}).get("model", {})
            model_name = model.get("name", "")
            found.append({
                "deployment_name": d.get("name", ""),
                "model_name": model_name,
                "model_version": model.get("version", ""),
                "is_realtime": model_name in realtime_models,
            })

        has_realtime = any(f["is_realtime"] for f in found)
        return web.json_response({
            "valid": has_realtime,
            "message": (
                "Realtime model deployment found"
                if has_realtime
                else "No realtime model deployment found. Deploy gpt-realtime-mini or gpt-4o-realtime-preview."
            ),
            "deployments": found,
        })

    # ------------------------------------------------------------------
    # Discovery: ACS
    # ------------------------------------------------------------------

    async def list_acs(self, _req: web.Request) -> web.Response:
        resources = await run_sync(self._az.json, "communication", "list")
        if not isinstance(resources, list):
            return web.json_response([])

        return web.json_response([
            {
                "name": r.get("name", ""),
                "resource_group": r.get("resourceGroup", ""),
                "location": r.get("location", ""),
            }
            for r in resources
        ])

    async def list_acs_phones(self, req: web.Request) -> web.Response:
        name = req.query.get("name", "").strip()
        rg = req.query.get("resource_group", "").strip()
        if not name or not rg:
            return _error("name and resource_group are required", 400)

        keys = await run_sync(
            self._az.json, "communication", "list-key",
            "--name", name, "--resource-group", rg,
        )
        conn_str = keys.get("primaryConnectionString", "") if isinstance(keys, dict) else ""
        if not conn_str:
            return web.json_response([])

        phones = await run_sync(
            self._az.json, "communication", "phonenumber", "list",
            "--connection-string", conn_str,
        )
        if not isinstance(phones, list):
            return web.json_response([])

        return web.json_response([
            {"phone_number": p.get("phoneNumber", "")}
            for p in phones
            if p.get("phoneNumber")
        ])

    # ------------------------------------------------------------------
    # Connect existing
    # ------------------------------------------------------------------

    async def connect_existing(self, req: web.Request) -> web.Response:
        body = await req.json()
        steps: list[dict] = []

        aoai_name = body.get("aoai_name", "").strip()
        aoai_rg = body.get("aoai_resource_group", "").strip()
        aoai_deployment = body.get("aoai_deployment", "").strip() or "gpt-realtime-mini"

        if not aoai_name or not aoai_rg:
            return _error("aoai_name and aoai_resource_group are required", 400)

        aoai_info = await run_sync(
            self._az.json, "cognitiveservices", "account", "show",
            "--name", aoai_name, "--resource-group", aoai_rg,
        )
        if not isinstance(aoai_info, dict):
            return _error(f"Azure OpenAI resource '{aoai_name}' not found in RG '{aoai_rg}'", 404)

        aoai_endpoint = aoai_info.get("properties", {}).get("endpoint", "")
        steps.append({"step": "aoai_resource", "status": "ok", "name": f"{aoai_name} (existing)"})

        deployments = await run_sync(
            self._az.json, "cognitiveservices", "account", "deployment", "list",
            "--name", aoai_name, "--resource-group", aoai_rg,
        )
        dep_found = isinstance(deployments, list) and any(
            d.get("name") == aoai_deployment for d in deployments
        )
        if not dep_found:
            steps.append({
                "step": "aoai_deployment", "status": "failed",
                "name": aoai_deployment,
                "detail": f"Deployment '{aoai_deployment}' not found on {aoai_name}",
            })
            return _voice_fail(steps)

        steps.append({"step": "aoai_deployment", "status": "ok", "name": f"{aoai_deployment} (verified)"})

        aoai_keys = await run_sync(
            self._az.json, "cognitiveservices", "account", "keys", "list",
            "--name", aoai_name, "--resource-group", aoai_rg,
        )
        aoai_key = aoai_keys.get("key1", "") if isinstance(aoai_keys, dict) else ""
        if aoai_key:
            steps.append({"step": "aoai_keys", "status": "ok"})
        else:
            logger.info("AOAI key retrieval skipped (disableLocalAuth likely true)")
            steps.append({
                "step": "aoai_keys", "status": "ok",
                "detail": "Key-based auth disabled; will use Entra ID (DefaultAzureCredential)",
            })

        acs_name = body.get("acs_name", "").strip()
        acs_rg = body.get("acs_resource_group", "").strip()
        conn_str = ""
        voice_rg = aoai_rg

        if acs_name and acs_rg:
            keys = await run_sync(
                self._az.json, "communication", "list-key",
                "--name", acs_name, "--resource-group", acs_rg,
            )
            conn_str = keys.get("primaryConnectionString", "") if isinstance(keys, dict) else ""
            if not conn_str:
                steps.append({
                    "step": "acs_resource", "status": "failed",
                    "name": acs_name, "detail": "Cannot retrieve connection string",
                })
                return _voice_fail(steps)
            steps.append({"step": "acs_resource", "status": "ok", "name": f"{acs_name} (existing)"})
            voice_rg = acs_rg
        else:
            voice_rg = aoai_rg
            if not await ensure_rg(self._az, voice_rg, "Global", steps):
                return _voice_fail(steps)
            acs_name, conn_str = await create_acs(self._az, voice_rg, steps)
            if not conn_str:
                return _voice_fail(steps)

        location = aoai_info.get("location", "swedencentral")

        if not aoai_key:
            await ensure_rbac(self._az, aoai_name, aoai_rg, steps)

        persist_config(
            self._store, voice_rg, location, acs_name, conn_str,
            aoai_name, aoai_endpoint, aoai_key, aoai_deployment, steps,
        )

        phone = body.get("phone_number", "").strip()
        if phone:
            self._store.save_voice_call(acs_source_number=phone)
            cfg.write_env(ACS_SOURCE_NUMBER=phone)
            steps.append({"step": "phone_number", "status": "ok", "name": phone})

        target = body.get("target_number", "").strip()
        if target:
            self._store.save_voice_call(voice_target_number=target)
            cfg.write_env(VOICE_TARGET_NUMBER=target)
            steps.append({"step": "target_number", "status": "ok", "name": target})

        logger.info("Voice connect completed: acs=%s, aoai=%s", acs_name, aoai_name)

        reinit = req.app.get("_reinit_voice")
        if reinit:
            reinit()

        return web.json_response({
            "status": "ok",
            "steps": steps,
            "message": "Connected to existing Azure resources.",
        })


def _voice_fail(steps: list[dict]) -> web.Response:
    failed = [s for s in steps if s.get("status") == "failed"]
    msg = failed[0].get("name", "Unknown step") if failed else "Unknown error"
    return web.json_response(
        {"status": "error", "steps": steps, "message": f"Voice deploy failed at: {msg}"},
    )
