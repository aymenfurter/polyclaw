"""Voice infrastructure provisioning helpers.

Standalone functions extracted from ``VoiceSetupRoutes`` for ACS + AOAI
resource creation, RBAC assignment, and configuration persistence.
"""

from __future__ import annotations

import functools
import logging
import secrets

from ...config.settings import cfg
from ...services.cloud.azure import AzureCLI
from ...state.infra_config import InfraConfigStore
from ...util.async_helpers import run_sync

logger = logging.getLogger(__name__)


async def ensure_rbac(
    az: AzureCLI, aoai_name: str, rg: str, steps: list[dict],
) -> None:
    """Assign *Cognitive Services OpenAI User* role to the current principal."""
    account = az.account_info()
    if not account:
        steps.append({
            "step": "rbac_assign", "status": "skip",
            "detail": "Cannot determine current principal (az account show failed)",
        })
        return

    principal_id = ""
    principal_type = "User"

    user_info = await run_sync(
        functools.partial(az.json, "ad", "signed-in-user", "show", quiet=True),
    )
    if isinstance(user_info, dict) and user_info.get("id"):
        principal_id = user_info["id"]
    else:
        sp_id = account.get("user", {}).get("name", "")
        if sp_id:
            sp_info = await run_sync(
                functools.partial(az.json, "ad", "sp", "show", "--id", sp_id, quiet=True),
            )
            if isinstance(sp_info, dict) and sp_info.get("id"):
                principal_id = sp_info["id"]
                principal_type = "ServicePrincipal"

    if not principal_id:
        steps.append({
            "step": "rbac_assign", "status": "skip",
            "detail": "Cannot determine principal ID for RBAC assignment",
        })
        return

    aoai_info = await run_sync(
        az.json, "cognitiveservices", "account", "show",
        "--name", aoai_name, "--resource-group", rg,
    )
    scope = aoai_info.get("id", "") if isinstance(aoai_info, dict) else ""
    if not scope:
        steps.append({
            "step": "rbac_assign", "status": "skip",
            "detail": "Cannot resolve resource ID for %s" % aoai_name,
        })
        return

    role = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
    logger.info("Assigning Cognitive Services OpenAI User role: principal=%s", principal_id)
    ok, msg = await run_sync(
        az.ok, "role", "assignment", "create",
        "--assignee-object-id", principal_id,
        "--assignee-principal-type", principal_type,
        "--role", role, "--scope", scope,
    )
    if ok:
        steps.append({"step": "rbac_assign", "status": "ok",
                      "detail": "Cognitive Services OpenAI User"})
    elif "already exists" in (msg or "").lower() or "conflict" in (msg or "").lower():
        steps.append({"step": "rbac_assign", "status": "ok", "detail": "Already assigned"})
    else:
        steps.append({
            "step": "rbac_assign", "status": "warning",
            "detail": "Role assignment failed (non-fatal): %s" % msg,
        })
        logger.warning("RBAC role assignment failed (non-fatal): %s", msg)


async def ensure_rg(
    az: AzureCLI, rg: str, location: str, steps: list[dict],
) -> bool:
    """Ensure a resource group exists, creating it if necessary."""
    existing = await run_sync(az.json, "group", "show", "--name", rg)
    if existing:
        steps.append({"step": "resource_group", "status": "ok",
                      "name": "%s (existing)" % rg})
        return True

    result = await run_sync(
        az.json, "group", "create", "--name", rg, "--location", location,
    )
    steps.append({"step": "resource_group",
                  "status": "ok" if result else "failed", "name": rg})
    if not result:
        logger.error("Voice deploy FAILED at resource group creation: %s", az.last_stderr)
    return bool(result)


async def create_acs(
    az: AzureCLI, rg: str, steps: list[dict],
) -> tuple[str, str]:
    """Create an ACS resource and retrieve its connection string."""
    acs_name = "polyclaw-acs-%s" % secrets.token_hex(4)
    acs = await run_sync(
        az.json, "communication", "create",
        "--name", acs_name, "--location", "Global",
        "--data-location", "United States", "--resource-group", rg,
    )
    steps.append({"step": "acs_resource",
                  "status": "ok" if acs else "failed", "name": acs_name})
    if not acs:
        logger.error("Voice deploy FAILED at ACS creation: %s", az.last_stderr)
        return "", ""

    keys = await run_sync(
        az.json, "communication", "list-key",
        "--name", acs_name, "--resource-group", rg,
    )
    conn_str = keys.get("primaryConnectionString", "") if isinstance(keys, dict) else ""
    steps.append({"step": "acs_keys", "status": "ok" if conn_str else "failed"})
    if not conn_str:
        logger.error("Voice deploy FAILED retrieving ACS keys: %s", az.last_stderr)
        return acs_name, ""
    return acs_name, conn_str


async def create_aoai(
    az: AzureCLI, rg: str, location: str, steps: list[dict],
) -> tuple[str, str, str, str]:
    """Create an Azure OpenAI resource with a realtime model deployment.

    Returns ``(name, endpoint, key, deployment_name)``.
    """
    aoai_name = "polyclaw-aoai-%s" % secrets.token_hex(4)
    deployment_name = "gpt-realtime-mini"

    aoai = await run_sync(
        az.json, "cognitiveservices", "account", "create",
        "--name", aoai_name, "--resource-group", rg,
        "--location", location, "--kind", "OpenAI",
        "--sku", "S0", "--custom-domain", aoai_name,
    )
    steps.append({"step": "aoai_resource",
                  "status": "ok" if aoai else "failed", "name": aoai_name})
    if not aoai:
        logger.error("Voice deploy FAILED at AOAI creation: %s", az.last_stderr)
        return "", "", "", ""

    dep = await run_sync(
        az.json, "cognitiveservices", "account", "deployment", "create",
        "--name", aoai_name, "--resource-group", rg,
        "--deployment-name", deployment_name,
        "--model-name", "gpt-realtime-mini",
        "--model-version", "2025-10-06",
        "--model-format", "OpenAI",
        "--sku-capacity", "1", "--sku-name", "GlobalStandard",
    )
    steps.append({"step": "aoai_deployment",
                  "status": "ok" if dep else "failed", "name": deployment_name})
    if not dep:
        logger.error("Voice deploy FAILED at model deployment: %s", az.last_stderr)
        return aoai_name, "", "", ""

    aoai_info = await run_sync(
        az.json, "cognitiveservices", "account", "show",
        "--name", aoai_name, "--resource-group", rg,
    )
    aoai_endpoint = ""
    if isinstance(aoai_info, dict):
        aoai_endpoint = aoai_info.get("properties", {}).get("endpoint", "")

    aoai_keys = await run_sync(
        az.json, "cognitiveservices", "account", "keys", "list",
        "--name", aoai_name, "--resource-group", rg,
    )
    aoai_key = aoai_keys.get("key1", "") if isinstance(aoai_keys, dict) else ""

    if not aoai_endpoint:
        steps.append({"step": "aoai_keys", "status": "failed"})
        logger.error("Voice deploy FAILED retrieving AOAI endpoint")
        return aoai_name, "", "", ""

    if aoai_key:
        steps.append({"step": "aoai_keys", "status": "ok"})
    else:
        steps.append({"step": "aoai_keys", "status": "ok",
                      "detail": "Using Entra ID auth"})

    return aoai_name, aoai_endpoint, aoai_key, deployment_name


def persist_config(
    store: InfraConfigStore,
    voice_rg: str,
    location: str,
    acs_name: str,
    conn_str: str,
    aoai_name: str,
    aoai_endpoint: str,
    aoai_key: str,
    deployment_name: str,
    steps: list[dict],
) -> None:
    """Write voice configuration to the infra config store and ``.env``."""
    store.save_voice_call(
        acs_resource_name=acs_name,
        acs_connection_string=conn_str,
        azure_openai_resource_name=aoai_name,
        azure_openai_endpoint=aoai_endpoint,
        azure_openai_api_key=aoai_key,
        azure_openai_realtime_deployment=deployment_name,
        resource_group=voice_rg,
        voice_resource_group=voice_rg,
        location=location,
    )
    callback_token = cfg.acs_callback_token
    cfg.write_env(
        ACS_CONNECTION_STRING=conn_str,
        ACS_SOURCE_NUMBER="",
        AZURE_OPENAI_ENDPOINT=aoai_endpoint,
        AZURE_OPENAI_API_KEY=aoai_key,
        AZURE_OPENAI_REALTIME_DEPLOYMENT=deployment_name,
        ACS_CALLBACK_TOKEN=callback_token,
    )
    steps.append({"step": "persist_config", "status": "ok"})
