"""Bicep-based infrastructure deployer.

Replaces the ad-hoc ``az`` CLI provisioning scattered across the codebase
with a single ``az deployment group create`` driven by ``infra/main.bicep``.
All resource creation is parameterised from internal config state.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config.settings import cfg
from ...state.deploy_state import DeployStateStore, DeploymentRecord, ResourceEntry, generate_deploy_id
from ..cloud.azure import AzureCLI

logger = logging.getLogger(__name__)


def _find_bicep_template() -> Path:
    """Locate infra/main.bicep by walking up from this file."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "infra" / "main.bicep"
        if candidate.exists():
            return candidate
    # Fallback for local dev layout
    return here.parents[4] / "infra" / "main.bicep"


_BICEP_TEMPLATE = _find_bicep_template()


@dataclass
class BicepDeployRequest:
    """Parameters for a Bicep deployment.

    Every ``deploy_*`` flag gates an optional resource block in the
    Bicep template.  Callers enable only the subset they need.
    """

    resource_group: str = "polyclaw-rg"
    location: str = "eastus"
    base_name: str = ""

    # Foundry (AI Services) + model deployments
    deploy_foundry: bool = True
    models: list[dict[str, Any]] = field(default_factory=lambda: [
        {"name": "gpt-4.1", "version": "2025-04-14", "sku": "GlobalStandard", "capacity": 10},
        {"name": "gpt-5", "version": "2025-08-07", "sku": "GlobalStandard", "capacity": 10},
        {"name": "gpt-5-mini", "version": "2025-08-07", "sku": "GlobalStandard", "capacity": 10},
    ])

    # Key Vault
    deploy_key_vault: bool = True

    # ACS (voice)
    deploy_acs: bool = False
    acs_data_location: str = "United States"

    # Content Safety
    deploy_content_safety: bool = False

    # Azure AI Search (Foundry IQ)
    deploy_search: bool = False

    # Embedding Azure OpenAI (Foundry IQ)
    deploy_embedding_aoai: bool = False
    embedding_model_name: str = "text-embedding-3-large"
    embedding_model_version: str = "1"

    # Log Analytics + Application Insights
    deploy_monitoring: bool = False

    # Container Apps session pool (sandbox)
    deploy_session_pool: bool = False

    def __post_init__(self) -> None:
        if not self.base_name:
            self.base_name = "polyclaw-%s" % secrets.token_hex(4)


@dataclass
class BicepDeployResult:
    """Result from a Bicep deployment."""

    ok: bool = False
    deploy_id: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    # Foundry
    foundry_endpoint: str = ""
    foundry_name: str = ""
    foundry_resource_id: str = ""
    deployed_models: list[str] = field(default_factory=list)

    # Key Vault
    key_vault_url: str = ""
    key_vault_name: str = ""

    # ACS
    acs_name: str = ""

    # Content Safety
    content_safety_endpoint: str = ""
    content_safety_name: str = ""
    content_safety_resource_id: str = ""

    # Azure AI Search
    search_endpoint: str = ""
    search_name: str = ""

    # Embedding Azure OpenAI
    embedding_aoai_endpoint: str = ""
    embedding_aoai_name: str = ""
    embedding_deployment_name: str = ""

    # Monitoring
    app_insights_connection_string: str = ""
    app_insights_name: str = ""
    log_analytics_workspace_name: str = ""

    # Sandbox
    session_pool_endpoint: str = ""
    session_pool_id: str = ""
    session_pool_name: str = ""


class _ObservableSteps(list):
    """A list subclass that fires a callback on every ``append``."""

    def __init__(self, callback: Callable[[dict[str, str]], None] | None = None) -> None:
        super().__init__()
        self._cb = callback

    def append(self, item: Any) -> None:  # type: ignore[override]
        super().append(item)
        if self._cb is not None:
            try:
                self._cb(item)
            except Exception:
                pass  # never let callback errors abort the deploy


class BicepDeployer:
    """Orchestrates infrastructure via a single Bicep template."""

    def __init__(
        self,
        az: AzureCLI,
        deploy_store: DeployStateStore,
    ) -> None:
        self._az = az
        self._store = deploy_store

    # -- public API --------------------------------------------------------

    def deploy(
        self,
        req: BicepDeployRequest,
        on_step: Callable[[dict[str, str]], None] | None = None,
    ) -> BicepDeployResult:
        """Run the full Bicep deployment and persist results.

        *on_step*, when supplied, is called synchronously every time a new
        progress step is recorded.  This enables streaming (e.g. SSE) from
        the route handler.
        """
        result = BicepDeployResult()
        deploy_id = generate_deploy_id()
        result.deploy_id = deploy_id

        # Wrap the steps list so that append() also fires the callback.
        steps = _ObservableSteps(on_step)
        result.steps = steps  # type: ignore[assignment]

        # 1. Ensure resource group
        if not self._ensure_resource_group(req, steps):
            result.error = "Resource group creation failed"
            return result

        # 2. Resolve principal for RBAC
        principal_id, principal_type = self._resolve_principal(steps)
        if not principal_id:
            result.error = "Cannot determine current principal for RBAC"
            return result

        # 2b. Ensure a runtime service principal exists.
        # The runtime container needs its own identity for:
        #   - Key Vault secret resolution (when KV is deployed)
        #   - Foundry BYOK bearer tokens (az account get-access-token)
        needs_sp = req.deploy_key_vault or req.deploy_foundry
        runtime_sp = self._ensure_runtime_sp(req, steps) if needs_sp else None

        # 3. Run Bicep deployment
        runtime_sp_oid = runtime_sp["object_id"] if runtime_sp else ""
        outputs = self._run_bicep(req, principal_id, principal_type, runtime_sp_oid, steps)
        if outputs is None:
            result.error = "Bicep deployment failed"
            return result

        # 4. Extract outputs
        def _out(key: str) -> str:
            return outputs.get(key, {}).get("value", "")

        def _out_list(key: str) -> list[str]:
            val = outputs.get(key, {}).get("value", [])
            return val if isinstance(val, list) else []

        result.foundry_endpoint = _out("foundryEndpoint")
        result.foundry_name = _out("foundryName")
        result.foundry_resource_id = _out("foundryResourceId")
        result.deployed_models = _out_list("deployedModels")
        result.key_vault_url = _out("keyVaultUrl")
        result.key_vault_name = _out("keyVaultName")
        result.acs_name = _out("acsName")
        result.content_safety_endpoint = _out("contentSafetyEndpoint")
        result.content_safety_name = _out("contentSafetyName")
        result.content_safety_resource_id = _out("contentSafetyResourceId")
        result.search_endpoint = _out("searchEndpoint")
        result.search_name = _out("searchName")
        result.embedding_aoai_endpoint = _out("embeddingAoaiEndpoint")
        result.embedding_aoai_name = _out("embeddingAoaiName")
        result.embedding_deployment_name = _out("embeddingDeploymentName")
        result.app_insights_connection_string = _out("appInsightsConnectionString")
        result.app_insights_name = _out("appInsightsName")
        result.log_analytics_workspace_name = _out("logAnalyticsWorkspaceName")
        result.session_pool_endpoint = _out("sessionPoolEndpoint")
        result.session_pool_id = _out("sessionPoolId")
        result.session_pool_name = _out("sessionPoolName")
        steps.append({"step": "extract_outputs", "status": "ok"})

        # 5. Persist to .env and state store
        self._persist(req, result, deploy_id, steps, runtime_sp=runtime_sp)

        result.ok = True
        logger.info(
            "[bicep.deploy] completed: endpoint=%s models=%s kv=%s",
            result.foundry_endpoint, result.deployed_models, result.key_vault_url,
        )
        return result

    def status(self) -> dict[str, Any]:
        """Return current Foundry deployment status from .env."""
        deployed_raw = cfg.env.read("DEPLOYED_MODELS") or ""
        deployed_models = [m.strip() for m in deployed_raw.split(",") if m.strip()]
        return {
            "deployed": bool(cfg.env.read("FOUNDRY_ENDPOINT")),
            "foundry_endpoint": cfg.env.read("FOUNDRY_ENDPOINT") or "",
            "foundry_name": cfg.env.read("FOUNDRY_NAME") or "",
            "foundry_resource_group": cfg.env.read("FOUNDRY_RESOURCE_GROUP") or "",
            "deployed_models": deployed_models,
            "key_vault_url": cfg.env.read("KEY_VAULT_URL") or "",
            "key_vault_name": cfg.env.read("KEY_VAULT_NAME") or "",
            "content_safety_endpoint": cfg.env.read("CONTENT_SAFETY_ENDPOINT") or "",
            "content_safety_name": cfg.env.read("CONTENT_SAFETY_NAME") or "",
            "search_endpoint": cfg.env.read("SEARCH_ENDPOINT") or "",
            "search_name": cfg.env.read("SEARCH_NAME") or "",
            "embedding_aoai_endpoint": cfg.env.read("EMBEDDING_AOAI_ENDPOINT") or "",
            "embedding_aoai_name": cfg.env.read("EMBEDDING_AOAI_NAME") or "",
            "app_insights_name": cfg.env.read("APP_INSIGHTS_NAME") or "",
            "session_pool_name": cfg.env.read("SESSION_POOL_NAME") or "",
            "acs_name": cfg.env.read("ACS_RESOURCE_NAME") or "",
            "bot_name": cfg.env.read("BOT_NAME") or "",
            "model": cfg.copilot_model,
        }

    def decommission(self, resource_group: str = "") -> list[dict[str, Any]]:
        """Delete the resource group (cascade deletes everything)."""
        rg = resource_group or cfg.env.read("FOUNDRY_RESOURCE_GROUP") or ""
        steps: list[dict[str, Any]] = []
        if not rg:
            steps.append({"step": "decommission", "status": "skip", "detail": "No RG configured"})
            return steps

        ok, msg = self._az.ok(
            "group", "delete", "--name", rg, "--yes", "--no-wait",
        )
        steps.append({
            "step": "delete_resource_group",
            "status": "ok" if ok else "failed",
            "detail": rg if ok else msg,
        })

        if ok:
            # Clean up the runtime service principal
            sp_app_id = cfg.env.read("RUNTIME_SP_APP_ID") or ""
            if sp_app_id:
                del_ok, del_msg = self._az.ok("ad", "sp", "delete", "--id", sp_app_id)
                steps.append({
                    "step": "delete_runtime_sp",
                    "status": "ok" if del_ok else "warning",
                    "detail": sp_app_id if del_ok else del_msg,
                })

            cfg.write_env(
                FOUNDRY_ENDPOINT="",
                FOUNDRY_NAME="",
                FOUNDRY_RESOURCE_GROUP="",
                KEY_VAULT_URL="",
                KEY_VAULT_NAME="",
                KEY_VAULT_RG="",
                RUNTIME_SP_APP_ID="",
                RUNTIME_SP_PASSWORD="",
                RUNTIME_SP_TENANT="",
            )
            steps.append({"step": "clear_env", "status": "ok"})

        return steps

    # -- internal helpers --------------------------------------------------

    def _ensure_resource_group(
        self, req: BicepDeployRequest, steps: list[dict],
    ) -> bool:
        existing = self._az.json("group", "show", "--name", req.resource_group, quiet=True)
        if existing:
            steps.append({"step": "resource_group", "status": "ok",
                          "detail": "%s (existing)" % req.resource_group})
            return True

        result = self._az.json(
            "group", "create",
            "--name", req.resource_group,
            "--location", req.location,
        )
        ok = bool(result)
        steps.append({
            "step": "resource_group",
            "status": "ok" if ok else "failed",
            "detail": req.resource_group,
        })
        if not ok:
            logger.error("RG creation failed: %s", self._az.last_stderr)
        return ok

    def _resolve_principal(self, steps: list[dict]) -> tuple[str, str]:
        """Return ``(principal_id, principal_type)`` for the signed-in identity."""
        account = self._az.account_info()
        if not account:
            steps.append({"step": "resolve_principal", "status": "failed",
                          "detail": "Not logged in"})
            return "", ""

        # Try user principal first
        user_info = self._az.json("ad", "signed-in-user", "show", quiet=True)
        if isinstance(user_info, dict) and user_info.get("id"):
            steps.append({"step": "resolve_principal", "status": "ok",
                          "detail": "User: %s" % user_info.get("userPrincipalName", "")})
            return user_info["id"], "User"

        # Fall back to service principal
        sp_name = account.get("user", {}).get("name", "")
        if sp_name:
            sp_info = self._az.json("ad", "sp", "show", "--id", sp_name, quiet=True)
            if isinstance(sp_info, dict) and sp_info.get("id"):
                steps.append({"step": "resolve_principal", "status": "ok",
                              "detail": "ServicePrincipal: %s" % sp_name})
                return sp_info["id"], "ServicePrincipal"

        steps.append({"step": "resolve_principal", "status": "failed",
                      "detail": "Cannot determine principal"})
        return "", ""

    def _ensure_runtime_sp(
        self, req: BicepDeployRequest, steps: list[dict],
    ) -> dict[str, str] | None:
        """Create or reuse a service principal for the runtime container.

        The runtime container needs its own Azure identity to resolve
        Key Vault secrets.  This method:

        1. Checks if ``RUNTIME_SP_APP_ID`` is already configured and valid.
        2. If not, creates a new SP via ``az ad sp create-for-rbac`` scoped
           to the resource group.
        3. Returns ``{app_id, password, tenant, object_id}`` for Bicep RBAC
           and ``.env`` persistence.
        """
        # Reuse existing SP if configured and valid
        existing_id = cfg.env.read("RUNTIME_SP_APP_ID") or ""
        existing_pw = cfg.env.read("RUNTIME_SP_PASSWORD") or ""
        existing_tenant = cfg.env.read("RUNTIME_SP_TENANT") or ""
        if existing_id and existing_pw and existing_tenant:
            sp_info = self._az.json("ad", "sp", "show", "--id", existing_id, quiet=True)
            if isinstance(sp_info, dict) and sp_info.get("id"):
                steps.append({
                    "step": "runtime_sp", "status": "ok",
                    "detail": "Reusing existing SP: %s" % existing_id,
                })
                return {
                    "app_id": existing_id,
                    "password": existing_pw,
                    "tenant": existing_tenant,
                    "object_id": sp_info["id"],
                }
            logger.warning(
                "[bicep.runtime_sp] existing SP %s not found in AD; creating new one",
                existing_id,
            )

        # Create a new SP scoped to the resource group
        scope = "/subscriptions/%s/resourceGroups/%s" % (
            self._az.account_info().get("id", ""),
            req.resource_group,
        )
        sp_name = "polyclaw-runtime-%s" % req.base_name

        # Try creating the SP with a 1-year credential.  If the tenant
        # policy rejects the lifetime, fall back to creating the SP without
        # a password and then adding a short-lived credential separately.
        sp: dict | list | None = None
        sp = self._az.json(
            "ad", "sp", "create-for-rbac",
            "--name", sp_name,
            "--role", "Reader",
            "--scopes", scope,
        )

        if not isinstance(sp, dict) or not sp.get("appId"):
            if "Credential lifetime" in (self._az.last_stderr or ""):
                logger.info("[bicep.runtime_sp] tenant restricts cred lifetime; using short-lived")
                # Create SP without password
                sp = self._az.json(
                    "ad", "sp", "create-for-rbac",
                    "--name", sp_name,
                    "--role", "Reader",
                    "--scopes", scope,
                    "--create-password", "false",
                )
                if isinstance(sp, dict) and sp.get("appId"):
                    from datetime import datetime, timedelta
                    end_date = (datetime.utcnow() + timedelta(days=90)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                    )
                    cred = self._az.json(
                        "ad", "app", "credential", "reset",
                        "--id", sp["appId"],
                        "--end-date", end_date,
                    )
                    if isinstance(cred, dict) and cred.get("password"):
                        sp["password"] = cred["password"]
                        sp["tenant"] = cred.get("tenant", sp.get("tenant", ""))
                    else:
                        steps.append({
                            "step": "runtime_sp", "status": "failed",
                            "detail": "Short-lived credential creation failed: %s"
                                      % self._az.last_stderr[:200],
                        })
                        return None

        if not isinstance(sp, dict) or not sp.get("appId"):
            steps.append({
                "step": "runtime_sp", "status": "failed",
                "detail": "az ad sp create-for-rbac failed: %s" % self._az.last_stderr[:300],
            })
            logger.error("[bicep.runtime_sp] SP creation failed: %s", self._az.last_stderr)
            return None

        # Resolve the SP's object ID (needed for Bicep RBAC assignment)
        sp_show = self._az.json("ad", "sp", "show", "--id", sp["appId"], quiet=True)
        object_id = sp_show["id"] if isinstance(sp_show, dict) and sp_show.get("id") else ""
        if not object_id:
            steps.append({
                "step": "runtime_sp", "status": "failed",
                "detail": "Could not resolve SP object ID for %s" % sp["appId"],
            })
            return None

        steps.append({
            "step": "runtime_sp", "status": "ok",
            "detail": "Created SP: %s (object_id=%s)" % (sp_name, object_id),
        })
        logger.info(
            "[bicep.runtime_sp] created: name=%s app_id=%s object_id=%s",
            sp_name, sp["appId"], object_id,
        )
        return {
            "app_id": sp["appId"],
            "password": sp.get("password", ""),
            "tenant": sp.get("tenant", ""),
            "object_id": object_id,
        }

    def _run_bicep(
        self,
        req: BicepDeployRequest,
        principal_id: str,
        principal_type: str,
        runtime_sp_object_id: str,
        steps: list[dict],
    ) -> dict[str, Any] | None:
        """Execute ``az deployment group create`` with the Bicep template."""
        if not _BICEP_TEMPLATE.exists():
            steps.append({"step": "bicep_deploy", "status": "failed",
                          "detail": "Template not found: %s" % _BICEP_TEMPLATE})
            logger.error("Bicep template not found at %s", _BICEP_TEMPLATE)
            return None

        params = {
            "baseName": {"value": req.base_name},
            "location": {"value": req.location},
            "principalId": {"value": principal_id},
            "principalType": {"value": principal_type},
            "deployFoundry": {"value": req.deploy_foundry},
            "models": {"value": req.models},
            "deployKeyVault": {"value": req.deploy_key_vault},
            "runtimeSpObjectId": {"value": runtime_sp_object_id},
            "deployAcs": {"value": req.deploy_acs},
            "acsDataLocation": {"value": req.acs_data_location},
            "deployContentSafety": {"value": req.deploy_content_safety},
            "deploySearch": {"value": req.deploy_search},
            "deployEmbeddingAoai": {"value": req.deploy_embedding_aoai},
            "embeddingModelName": {"value": req.embedding_model_name},
            "embeddingModelVersion": {"value": req.embedding_model_version},
            "deployMonitoring": {"value": req.deploy_monitoring},
            "deploySessionPool": {"value": req.deploy_session_pool},
        }
        params_json = json.dumps(params)

        deploy_name = "polyclaw-%s" % req.base_name

        logger.info(
            "[bicep.deploy] running: rg=%s base=%s models=%d kv=%s acs=%s",
            req.resource_group, req.base_name, len(req.models),
            req.deploy_key_vault, req.deploy_acs,
        )

        # Run the deployment — use --name so we can query it afterwards.
        result = self._az.json(
            "deployment", "group", "create",
            "--resource-group", req.resource_group,
            "--name", deploy_name,
            "--template-file", str(_BICEP_TEMPLATE),
            "--parameters", params_json,
        )

        # If the create command failed, it may be an Azure CLI response-parsing
        # bug (e.g. "The content for this response was already consumed" in
        # az 2.77.0).  Check if the deployment actually succeeded by querying it.
        if result is None:
            stderr = self._az.last_stderr
            logger.warning(
                "[bicep.deploy] create returned None; checking deployment status: %s",
                stderr[:200],
            )
            result = self._az.json(
                "deployment", "group", "show",
                "--resource-group", req.resource_group,
                "--name", deploy_name,
                "--query", "properties.outputs",
                quiet=True,
            )
            if result is None:
                steps.append({"step": "bicep_deploy", "status": "failed",
                              "detail": stderr[:500]})
                logger.error("Bicep deployment failed: %s", stderr)
                return None

            logger.info("[bicep.deploy] deployment found via fallback query")
        else:
            # Extract outputs from the inline response
            if isinstance(result, dict):
                result = result.get("properties", result).get("outputs", result)

        steps.append({"step": "bicep_deploy", "status": "ok",
                      "detail": "Deployment succeeded"})
        return result if isinstance(result, dict) else {}

    def _persist(
        self,
        req: BicepDeployRequest,
        result: BicepDeployResult,
        deploy_id: str,
        steps: list[dict],
        runtime_sp: dict[str, str] | None = None,
    ) -> None:
        """Write deployment outputs to .env and the deploy state store."""
        env_vars: dict[str, str] = {}

        if result.foundry_endpoint:
            env_vars.update({
                "FOUNDRY_ENDPOINT": result.foundry_endpoint,
                "FOUNDRY_NAME": result.foundry_name,
                "FOUNDRY_RESOURCE_GROUP": req.resource_group,
                "COPILOT_MODEL": (
                    result.deployed_models[0] if result.deployed_models else "gpt-4.1"
                ),
                "DEPLOYED_MODELS": ",".join(result.deployed_models),
            })
        if result.key_vault_url:
            env_vars.update({
                "KEY_VAULT_URL": result.key_vault_url,
                "KEY_VAULT_NAME": result.key_vault_name,
                "KEY_VAULT_RG": req.resource_group,
            })
        if runtime_sp:
            env_vars.update({
                "RUNTIME_SP_APP_ID": runtime_sp["app_id"],
                "RUNTIME_SP_PASSWORD": runtime_sp["password"],
                "RUNTIME_SP_TENANT": runtime_sp["tenant"],
            })
        if result.content_safety_endpoint:
            env_vars.update({
                "CONTENT_SAFETY_ENDPOINT": result.content_safety_endpoint,
                "CONTENT_SAFETY_NAME": result.content_safety_name,
            })
        if result.search_endpoint:
            env_vars.update({
                "SEARCH_ENDPOINT": result.search_endpoint,
                "SEARCH_NAME": result.search_name,
            })
        if result.embedding_aoai_endpoint:
            env_vars.update({
                "EMBEDDING_AOAI_ENDPOINT": result.embedding_aoai_endpoint,
                "EMBEDDING_AOAI_NAME": result.embedding_aoai_name,
                "EMBEDDING_DEPLOYMENT_NAME": result.embedding_deployment_name,
            })
        if result.app_insights_connection_string:
            env_vars.update({
                "APP_INSIGHTS_CONNECTION_STRING": result.app_insights_connection_string,
                "APP_INSIGHTS_NAME": result.app_insights_name,
                "LOG_ANALYTICS_WORKSPACE_NAME": result.log_analytics_workspace_name,
            })
        if result.session_pool_endpoint:
            env_vars.update({
                "SESSION_POOL_ENDPOINT": result.session_pool_endpoint,
                "SESSION_POOL_ID": result.session_pool_id,
                "SESSION_POOL_NAME": result.session_pool_name,
            })
        if result.acs_name:
            env_vars["ACS_RESOURCE_NAME"] = result.acs_name

        if env_vars:
            cfg.write_env(**env_vars)
        steps.append({"step": "persist_env", "status": "ok"})

        # Auto-configure feature stores from deployment outputs
        self._configure_stores(req, result, steps)

        rec = DeploymentRecord(
            deploy_id=deploy_id,
            kind="local",
            status="active",
            resource_groups=[req.resource_group],
        )
        rec.resources = []

        _RESOURCE_MAP: list[tuple[bool, str, str, str]] = [
            (bool(result.foundry_name),
             "Microsoft.CognitiveServices/accounts",
             result.foundry_name, "Foundry AI Services"),
            (bool(result.key_vault_name),
             "Microsoft.KeyVault/vaults",
             result.key_vault_name, "Key Vault"),
            (bool(result.acs_name),
             "Microsoft.Communication/communicationServices",
             result.acs_name, "Communication Services"),
            (bool(result.content_safety_name),
             "Microsoft.CognitiveServices/accounts",
             result.content_safety_name, "Content Safety"),
            (bool(result.search_name),
             "Microsoft.Search/searchServices",
             result.search_name, "Azure AI Search"),
            (bool(result.embedding_aoai_name),
             "Microsoft.CognitiveServices/accounts",
             result.embedding_aoai_name, "Embedding Azure OpenAI"),
            (bool(result.app_insights_name),
             "Microsoft.Insights/components",
             result.app_insights_name, "Application Insights"),
            (bool(result.log_analytics_workspace_name),
             "Microsoft.OperationalInsights/workspaces",
             result.log_analytics_workspace_name, "Log Analytics Workspace"),
            (bool(result.session_pool_name),
             "Microsoft.App/sessionPools",
             result.session_pool_name, "Session Pool"),
        ]
        for enabled, rtype, rname, purpose in _RESOURCE_MAP:
            if enabled:
                rec.resources.append(ResourceEntry(
                    resource_type=rtype,
                    resource_group=req.resource_group,
                    resource_name=rname,
                    purpose=purpose,
                ))

        self._store.register(rec)
        steps.append({"step": "persist_state", "status": "ok"})

    def _configure_stores(
        self,
        req: BicepDeployRequest,
        result: BicepDeployResult,
        steps: list[dict],
    ) -> None:
        """Auto-configure feature JSON stores from Bicep outputs.

        After a one-click deploy the features should be immediately usable
        without any manual configuration steps in the admin GUI.
        """
        # -- Content Safety / Prompt Shields ----------------------------------
        if result.content_safety_endpoint:
            try:
                from ...state.guardrails.config import get_guardrails_config
                gs = get_guardrails_config()
                gs.set_content_safety_endpoint(result.content_safety_endpoint)
                gs.set_filter_mode("prompt_shields")
                steps.append({
                    "step": "configure_content_safety", "status": "ok",
                    "detail": result.content_safety_endpoint,
                })
            except Exception as exc:
                logger.warning("[bicep.configure] content safety: %s", exc, exc_info=True)
                steps.append({
                    "step": "configure_content_safety", "status": "failed",
                    "detail": str(exc)[:200],
                })

        # -- Foundry IQ (Azure AI Search + Embedding) -------------------------
        if result.search_endpoint and result.embedding_aoai_endpoint:
            try:
                self._configure_foundry_iq(req, result, steps)
            except Exception as exc:
                logger.warning("[bicep.configure] foundry_iq: %s", exc, exc_info=True)
                steps.append({
                    "step": "configure_foundry_iq", "status": "failed",
                    "detail": str(exc)[:200],
                })

        # -- Monitoring (App Insights + OTel) ---------------------------------
        if result.app_insights_connection_string:
            try:
                from ...state.monitoring_config import get_monitoring_config
                account = self._az.account_info()
                sub_id = account.get("id", "") if account else ""
                ms = get_monitoring_config()
                ms.set_provisioned_metadata(
                    app_insights_name=result.app_insights_name,
                    workspace_name=result.log_analytics_workspace_name,
                    resource_group=req.resource_group,
                    location=req.location,
                    connection_string=result.app_insights_connection_string,
                    subscription_id=sub_id,
                )
                steps.append({
                    "step": "configure_monitoring", "status": "ok",
                    "detail": result.app_insights_name,
                })
            except Exception as exc:
                logger.warning("[bicep.configure] monitoring: %s", exc, exc_info=True)
                steps.append({
                    "step": "configure_monitoring", "status": "failed",
                    "detail": str(exc)[:200],
                })

        # -- Sandbox (Session Pool) -------------------------------------------
        if result.session_pool_endpoint:
            try:
                from ...state.sandbox_config import get_sandbox_config
                ss = get_sandbox_config()
                ss.set_pool_metadata(
                    resource_group=req.resource_group,
                    location=req.location,
                    pool_name=result.session_pool_name,
                    pool_id=result.session_pool_id,
                    endpoint=result.session_pool_endpoint,
                )
                steps.append({
                    "step": "configure_session_pool", "status": "ok",
                    "detail": result.session_pool_name,
                })
            except Exception as exc:
                logger.warning("[bicep.configure] session pool: %s", exc, exc_info=True)
                steps.append({
                    "step": "configure_session_pool", "status": "failed",
                    "detail": str(exc)[:200],
                })

        # -- Voice / ACS -------------------------------------------------------
        if result.acs_name:
            try:
                from ...state.infra_config import get_infra_config
                # Fetch the ACS connection string for voice calling
                keys = self._az.json(
                    "communication", "list-key",
                    "--name", result.acs_name,
                    "--resource-group", req.resource_group,
                    quiet=True,
                )
                conn_string = (
                    keys.get("primaryConnectionString", "")
                    if isinstance(keys, dict) else ""
                )
                infra = get_infra_config()
                infra.save_voice_call(
                    acs_resource_name=result.acs_name,
                    acs_connection_string=conn_string,
                    resource_group=req.resource_group,
                    location=req.location,
                )
                steps.append({
                    "step": "configure_acs", "status": "ok",
                    "detail": result.acs_name,
                })
            except Exception as exc:
                logger.warning("[bicep.configure] acs: %s", exc, exc_info=True)
                steps.append({
                    "step": "configure_acs", "status": "failed",
                    "detail": str(exc)[:200],
                })

    def _configure_foundry_iq(
        self,
        req: BicepDeployRequest,
        result: BicepDeployResult,
        steps: list[dict],
    ) -> None:
        """Wire up Azure AI Search + Embedding AOAI for Foundry IQ."""
        from ...state.foundry_iq_config import get_foundry_iq_config

        # Managed-identity auth is preferred (Bicep assigns RBAC roles).
        # API keys are only used as a fallback when local-auth is enabled.
        search_key = ""
        aoai_key = ""

        fiq = get_foundry_iq_config()
        fiq.save(
            resource_group=req.resource_group,
            location=req.location,
            search_resource_name=result.search_name,
            openai_resource_name=result.embedding_aoai_name,
            openai_deployment_name=result.embedding_deployment_name,
            search_endpoint=result.search_endpoint,
            search_api_key=search_key,
            embedding_endpoint=result.embedding_aoai_endpoint,
            embedding_api_key=aoai_key,
            embedding_model=result.embedding_deployment_name,
            embedding_dimensions=3072,
            index_name="polyclaw-memories",
            provisioned=True,
            enabled=True,
        )
        steps.append({
            "step": "configure_foundry_iq", "status": "ok",
            "detail": "search=%s aoai=%s" % (result.search_name, result.embedding_aoai_name),
        })

        # Create the search index
        try:
            from ..foundry_iq import ensure_index
            idx_result = ensure_index(fiq)
            idx_ok = idx_result.get("status") == "ok"
            steps.append({
                "step": "create_search_index",
                "status": "ok" if idx_ok else "warning",
                "detail": idx_result.get("detail", ""),
            })
        except Exception as exc:
            steps.append({
                "step": "create_search_index", "status": "warning",
                "detail": str(exc)[:200],
            })
