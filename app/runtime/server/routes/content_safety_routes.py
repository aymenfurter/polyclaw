"""Admin routes for Azure AI Content Safety provisioning -- /api/content-safety/*."""

from __future__ import annotations

import functools
import logging
from typing import Any

from aiohttp import web

from ...config.settings import cfg
from ...services.cloud.azure import AzureCLI
from ...services.deployment.bicep_deployer import BicepDeployer, BicepDeployRequest
from ...services.security.prompt_shield import PromptShieldService
from ...state.deploy_state import DeployStateStore
from ...state.guardrails import GuardrailsConfigStore
from ...util.async_helpers import run_sync

logger = logging.getLogger(__name__)

_DEFAULT_RG = "polyclaw-rg"
_DEFAULT_LOCATION = "eastus"

# Built-in role: Cognitive Services User -- allows calling Content Safety APIs.
_COGNITIVE_SERVICES_USER_ROLE = "a97b65f3-24c7-4388-baec-2e87135dc908"


class ContentSafetyRoutes:
    """Provision and manage Azure AI Content Safety resources.

    Authentication always uses ``DefaultAzureCredential`` (Entra ID).
    API keys are never retrieved or stored.  During provisioning the
    runtime service principal is granted Cognitive Services User on the
    new resource so it can call the Prompt Shields API.
    """

    def __init__(
        self,
        az: AzureCLI | None = None,
        guardrails_store: GuardrailsConfigStore | None = None,
        deploy_store: DeployStateStore | None = None,
    ) -> None:
        self._az = az
        self._store = guardrails_store
        self._deploy_store = deploy_store
        self._bicep = BicepDeployer(az, deploy_store) if az and deploy_store else None

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_post("/api/content-safety/deploy", self._deploy)
        router.add_get("/api/content-safety/status", self._status)
        router.add_post("/api/content-safety/test", self._test)

    async def _status(self, _req: web.Request) -> web.Response:
        """Return current Content Safety configuration status."""
        if not self._store:
            return web.json_response({"status": "ok", "deployed": False})
        config = self._store.config
        return web.json_response({
            "status": "ok",
            "deployed": bool(config.content_safety_endpoint),
            "endpoint": config.content_safety_endpoint,
            "filter_mode": config.filter_mode,
        })

    async def _test(self, _req: web.Request) -> web.Response:
        """Dry-run: send a harmless probe to the Prompt Shields API.

        Returns ``{"status": "ok", "passed": true/false, "detail": "..."}``
        so the admin can verify that Entra ID auth and RBAC are set up
        correctly *before* relying on the shield to block attacks.
        """
        if not self._store:
            return web.json_response(
                {"status": "error", "message": "Guardrails store not available"},
                status=500,
            )

        endpoint = self._store.config.content_safety_endpoint
        if not endpoint:
            return web.json_response({
                "status": "ok",
                "passed": False,
                "detail": "No endpoint configured -- deploy first",
            })

        shield = PromptShieldService(endpoint=endpoint)
        result = await run_sync(shield.dry_run)
        passed = not result.attack_detected
        logger.info(
            "[content-safety.test] dry-run passed=%s detail=%s",
            passed, result.detail,
        )
        return web.json_response({
            "status": "ok",
            "passed": passed,
            "detail": result.detail,
        })

    async def _deploy(self, req: web.Request) -> web.Response:
        """Provision an Azure AI Content Safety resource via the central Bicep template."""
        if not self._bicep:
            return web.json_response(
                {"status": "error", "message": "Azure CLI or deploy store not available"},
                status=400,
            )
        if not self._store:
            return web.json_response(
                {"status": "error", "message": "Guardrails store not available"},
                status=500,
            )

        try:
            data = await req.json()
        except Exception:
            data = {}

        resource_group = data.get("resource_group", _DEFAULT_RG).strip()
        location = data.get("location", _DEFAULT_LOCATION).strip()

        bicep_req = BicepDeployRequest(
            resource_group=resource_group,
            location=location,
            deploy_foundry=False,
            deploy_key_vault=False,
            deploy_content_safety=True,
        )
        result = await run_sync(self._bicep.deploy, bicep_req)

        if not result.ok or not result.content_safety_endpoint:
            return web.json_response({
                "status": "error",
                "message": result.error or "Failed to deploy Content Safety resource",
                "steps": result.steps,
            }, status=500)

        # Update guardrails config
        self._store.set_content_safety_endpoint(result.content_safety_endpoint)
        self._store.set_filter_mode("prompt_shields")
        result.steps.append({
            "step": "update_config",
            "status": "ok",
            "detail": (
                "Config updated: endpoint set, mode=prompt_shields.  "
                "Auth uses managed identity (DefaultAzureCredential)."
            ),
        })

        return web.json_response({
            "status": "ok",
            "steps": result.steps,
            "endpoint": result.content_safety_endpoint,
            "filter_mode": "prompt_shields",
        })

    # ------------------------------------------------------------------
    # Public API -- called from admin startup
    # ------------------------------------------------------------------

    async def ensure_rbac(self) -> list[dict[str, Any]]:
        """Ensure the runtime identity has *Cognitive Services User* on the
        configured Content Safety resource.

        Called from ``_on_startup_admin`` so the RBAC assignment is
        always current -- even when the service principal changes or the
        role was revoked externally.

        Returns the list of step dicts (for logging); empty when there
        is nothing to do.
        """
        if not self._az or not self._store:
            return []

        endpoint = self._store.config.content_safety_endpoint
        if not endpoint:
            return []

        steps: list[dict[str, Any]] = []

        resource_id = await self._resolve_resource_id(endpoint, steps)
        if not resource_id:
            return steps

        await self._assign_rbac(resource_id, steps)
        return steps

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_resource_id(
        self,
        endpoint: str,
        steps: list[dict[str, Any]],
    ) -> str:
        """Find the ARM resource ID for a Content Safety endpoint.

        First tries listing within the configured resource group (fast),
        then falls back to a subscription-wide list.  Returns ``""`` on
        failure.
        """
        assert self._az is not None
        normalised = endpoint.rstrip("/").lower()

        rg = _DEFAULT_RG
        if rg:
            accounts = await run_sync(
                self._az.json,
                "cognitiveservices", "account", "list",
                "--resource-group", rg,
            )
            rid = self._match_endpoint(accounts, normalised)
            if rid:
                steps.append({
                    "step": "resolve_resource",
                    "status": "ok",
                    "detail": f"Resolved via resource-group {rg}",
                })
                return rid

        accounts = await run_sync(
            self._az.json, "cognitiveservices", "account", "list",
        )
        if not isinstance(accounts, list):
            steps.append({
                "step": "resolve_resource",
                "status": "warning",
                "detail": "Failed to list Cognitive Services accounts",
            })
            return ""

        rid = self._match_endpoint(accounts, normalised)
        if rid:
            steps.append({
                "step": "resolve_resource",
                "status": "ok",
                "detail": rid,
            })
            return rid

        steps.append({
            "step": "resolve_resource",
            "status": "warning",
            "detail": f"No account matched endpoint {endpoint}",
        })
        return ""

    @staticmethod
    def _match_endpoint(
        accounts: list[Any] | dict[str, Any] | None,
        normalised: str,
    ) -> str:
        """Return the ARM resource ID whose endpoint matches *normalised*."""
        if not isinstance(accounts, list):
            return ""
        for acct in accounts:
            if not isinstance(acct, dict):
                continue
            acct_ep = (
                acct.get("properties", {}).get("endpoint", "")
            ).rstrip("/").lower()
            if acct_ep == normalised:
                return acct.get("id", "")
        return ""

    async def _resolve_runtime_principal(
        self,
    ) -> tuple[str, str]:
        """Detect the runtime identity for RBAC assignment.

        Resolution order:
        1. ``RUNTIME_SP_APP_ID`` -- explicit service principal.
        2. ``ACA_MI_CLIENT_ID`` -- user-assigned managed identity.
        3. Current Azure CLI identity (signed-in user or SP).

        Returns ``(principal_object_id, principal_type)``.
        Either may be empty when detection fails.
        """
        assert self._az is not None

        # 1. Explicit service principal
        sp_app_id = cfg.runtime_sp_app_id
        if sp_app_id:
            sp_info = await run_sync(
                functools.partial(
                    self._az.json, "ad", "sp", "show", "--id", sp_app_id, quiet=True,
                ),
            )
            pid = ""
            if isinstance(sp_info, dict):
                pid = sp_info.get("id", "") or sp_info.get("objectId", "")
            if pid:
                return pid, "ServicePrincipal"
            logger.warning(
                "[content-safety.rbac] Cannot resolve object-id for "
                "RUNTIME_SP_APP_ID=%s, trying fallbacks",
                sp_app_id,
            )

        # 2. User-assigned managed identity
        mi_client_id = cfg.aca_mi_client_id
        if mi_client_id:
            mi_info = await run_sync(
                functools.partial(
                    self._az.json, "ad", "sp", "show", "--id", mi_client_id, quiet=True,
                ),
            )
            pid = ""
            if isinstance(mi_info, dict):
                pid = mi_info.get("id", "") or mi_info.get("objectId", "")
            if pid:
                return pid, "ServicePrincipal"
            logger.warning(
                "[content-safety.rbac] Cannot resolve object-id for "
                "ACA_MI_CLIENT_ID=%s, trying CLI identity",
                mi_client_id,
            )

        # 3. Current Azure CLI identity
        user_info = await run_sync(
            functools.partial(
                self._az.json, "ad", "signed-in-user", "show", quiet=True,
            ),
        )
        if isinstance(user_info, dict) and user_info.get("id"):
            return user_info["id"], "User"

        account = self._az.account_info()
        if account:
            name = account.get("user", {}).get("name", "")
            if name:
                sp_info = await run_sync(
                    functools.partial(
                        self._az.json, "ad", "sp", "show", "--id", name, quiet=True,
                    ),
                )
                if isinstance(sp_info, dict) and sp_info.get("id"):
                    return sp_info["id"], "ServicePrincipal"

        return "", ""

    async def _assign_rbac(
        self,
        resource_id: str,
        steps: list[dict[str, Any]],
    ) -> None:
        """Assign *Cognitive Services User* to the runtime identity."""
        assert self._az is not None

        principal_id, principal_type = await self._resolve_runtime_principal()
        if not principal_id:
            steps.append({
                "step": "rbac_assign",
                "status": "warning",
                "detail": (
                    "Cannot determine runtime identity -- "
                    "set RUNTIME_SP_APP_ID or ACA_MI_CLIENT_ID, "
                    "or assign Cognitive Services User manually"
                ),
            })
            return

        scope = resource_id or ""
        if not scope:
            steps.append({
                "step": "rbac_assign",
                "status": "warning",
                "detail": "No resource ID available for RBAC scope",
            })
            return

        logger.info(
            "[content-safety.rbac] Assigning Cognitive Services User: "
            "principal=%s type=%s scope=%s",
            principal_id, principal_type, scope,
        )
        ok, msg = await run_sync(
            self._az.ok,
            "role", "assignment", "create",
            "--assignee-object-id", principal_id,
            "--assignee-principal-type", principal_type,
            "--role", _COGNITIVE_SERVICES_USER_ROLE,
            "--scope", scope,
        )
        if ok:
            steps.append({
                "step": "rbac_assign",
                "status": "ok",
                "detail": f"Cognitive Services User assigned ({principal_type})",
            })
        elif "already exists" in (msg or "").lower() or "conflict" in (msg or "").lower():
            steps.append({
                "step": "rbac_assign",
                "status": "ok",
                "detail": "Already assigned",
            })
        else:
            steps.append({
                "step": "rbac_assign",
                "status": "warning",
                "detail": f"Role assignment failed (non-fatal): {msg}",
            })
            logger.warning(
                "[content-safety.rbac] RBAC assignment failed: %s",
                msg, exc_info=True,
            )
