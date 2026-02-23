"""Identity preflight checks (login gate, identity config, validity, credential expiry)."""

from __future__ import annotations

from datetime import datetime, timezone

from ...config.settings import cfg
from ..cloud.azure import AzureCLI
from .security_preflight import (
    IdentityInfo,
    PreflightCheck,
    PreflightResult,
    add_check as _add,
)


# -- Azure login gate ---------------------------------------------------

def check_azure_logged_in(az: AzureCLI, result: PreflightResult) -> bool:
    cmd = "az account show"
    account = az.json("account", "show", quiet=True)
    if isinstance(account, dict) and account.get("id"):
        sub = account.get("name", account.get("id", "?"))
        _add(
            result, id="azure_logged_in", category="identity",
            name="Azure CLI Authenticated",
            status="pass",
            detail=f"Logged in to subscription: {sub}",
            evidence=f"subscription={sub}\ntenantId={account.get('tenantId', '?')}",
            command=cmd,
        )
        return True
    _add(
        result, id="azure_logged_in", category="identity",
        name="Azure CLI Authenticated",
        status="fail",
        detail="Not logged in -- RBAC and identity checks require Azure CLI auth",
        evidence=az.last_stderr or "No response",
        command=cmd,
    )
    return False


def skip_azure_checks(result: PreflightResult) -> None:
    for check_id, name, cat in [
        ("identity_configured", "Runtime Identity Configured", "identity"),
        ("identity_valid", "Identity Exists in Azure AD", "identity"),
        ("identity_credential_expiry", "Credential Expiry", "identity"),
        ("rbac_assignments_list", "RBAC Assignments", "rbac"),
        ("rbac_bot_contributor", "Azure Bot Service Contributor Role", "rbac"),
        ("rbac_reader", "Reader Role", "rbac"),
        ("rbac_kv_access", "Key Vault Access Role", "rbac"),
        ("rbac_session_pool", "Session Pool Executor", "rbac"),
        ("rbac_no_elevated", "No Elevated Roles", "rbac"),
        ("rbac_scope_contained", "Scope Limited to Resource Group", "rbac"),
    ]:
        _add(
            result, id=check_id, category=cat, name=name,
            status="skip",
            detail="Skipped -- Azure CLI not authenticated",
            command="",
        )


# -- Identity checks ----------------------------------------------------

def check_identity_configured(
    az: AzureCLI, result: PreflightResult,
) -> IdentityInfo | None:
    sp_app_id = cfg.env.read("RUNTIME_SP_APP_ID")
    mi_client_id = cfg.env.read("ACA_MI_CLIENT_ID")
    mi_resource_id = cfg.env.read("ACA_MI_RESOURCE_ID")

    if mi_client_id:
        _add(
            result, id="identity_configured", category="identity",
            name="Runtime Identity Configured",
            status="pass",
            detail=f"User-assigned managed identity: client_id={mi_client_id}",
            evidence=(
                f"ACA_MI_CLIENT_ID={mi_client_id}\n"
                f"ACA_MI_RESOURCE_ID={mi_resource_id}"
            ),
            command="env: ACA_MI_CLIENT_ID, ACA_MI_RESOURCE_ID",
        )
        return {
            "strategy": "managed_identity",
            "client_id": mi_client_id,
            "resource_id": mi_resource_id,
            "assignee": mi_client_id,
        }

    if sp_app_id:
        sp_tenant = cfg.env.read("RUNTIME_SP_TENANT")
        has_pw = bool(cfg.env.read("RUNTIME_SP_PASSWORD"))
        _add(
            result, id="identity_configured", category="identity",
            name="Runtime Identity Configured",
            status="pass",
            detail=f"Scoped service principal: app_id={sp_app_id}",
            evidence=(
                f"RUNTIME_SP_APP_ID={sp_app_id}\n"
                f"RUNTIME_SP_TENANT={sp_tenant}\n"
                f"RUNTIME_SP_PASSWORD={'***' if has_pw else 'MISSING'}"
            ),
            command="env: RUNTIME_SP_APP_ID, RUNTIME_SP_TENANT, RUNTIME_SP_PASSWORD",
        )
        return {
            "strategy": "sp",
            "app_id": sp_app_id,
            "tenant": sp_tenant,
            "assignee": sp_app_id,
        }

    _add(
        result, id="identity_configured", category="identity",
        name="Runtime Identity Configured",
        status="skip",
        detail="No runtime identity configured (RUNTIME_SP_* and ACA_MI_* absent)",
        evidence="RUNTIME_SP_APP_ID=(empty)\nACA_MI_CLIENT_ID=(empty)",
        command="env: RUNTIME_SP_APP_ID, ACA_MI_CLIENT_ID",
    )
    return None


def check_identity_valid(
    az: AzureCLI, result: PreflightResult, info: IdentityInfo,
) -> None:
    if info["strategy"] == "sp":
        app_id = info["app_id"]
        cmd = f"az ad sp show --id {app_id}"
        sp = az.json("ad", "sp", "show", "--id", app_id)
        if isinstance(sp, dict) and sp.get("appId"):
            display = sp.get("displayName", "?")
            _add(
                result, id="identity_valid", category="identity",
                name="Service Principal Exists in Azure AD",
                status="pass",
                detail=f"{display} ({app_id})",
                evidence=(
                    f"displayName={display}\n"
                    f"appId={app_id}\n"
                    f"objectId={sp.get('id', '?')}"
                ),
                command=cmd,
            )
        else:
            _add(
                result, id="identity_valid", category="identity",
                name="Service Principal Exists in Azure AD",
                status="fail",
                detail=f"SP not found: {app_id}",
                evidence=az.last_stderr or "No response",
                command=cmd,
            )
    else:
        resource_id = info.get("resource_id", "")
        if not resource_id:
            _add(
                result, id="identity_valid", category="identity",
                name="Managed Identity Exists",
                status="skip", detail="No MI resource ID configured",
                command="",
            )
            return
        cmd = f"az identity show --ids {resource_id}"
        mi = az.json("identity", "show", "--ids", resource_id)
        if isinstance(mi, dict) and mi.get("clientId"):
            _add(
                result, id="identity_valid", category="identity",
                name="Managed Identity Exists",
                status="pass",
                detail=f"{mi.get('name', '?')} (client={mi.get('clientId', '?')})",
                evidence=(
                    f"name={mi.get('name', '?')}\n"
                    f"clientId={mi.get('clientId', '?')}\n"
                    f"principalId={mi.get('principalId', '?')}"
                ),
                command=cmd,
            )
        else:
            _add(
                result, id="identity_valid", category="identity",
                name="Managed Identity Exists",
                status="fail",
                detail=f"MI not found: {resource_id}",
                evidence=az.last_stderr or "No response",
                command=cmd,
            )


def check_credential_expiry(
    az: AzureCLI, result: PreflightResult, info: IdentityInfo,
) -> None:
    if info["strategy"] != "sp":
        _add(
            result, id="identity_credential_expiry", category="identity",
            name="Credential Expiry",
            status="pass",
            detail="Managed identities do not have expiring credentials",
            command="(not applicable for MI)",
        )
        return

    app_id = info["app_id"]
    cmd = f"az ad app credential list --id {app_id}"
    creds = az.json("ad", "app", "credential", "list", "--id", app_id)
    if not isinstance(creds, list) or not creds:
        _add(
            result, id="identity_credential_expiry", category="identity",
            name="Credential Expiry",
            status="warn",
            detail="Could not retrieve credential list",
            evidence=az.last_stderr or "Empty response",
            command=cmd,
        )
        return

    latest = max(creds, key=lambda c: c.get("endDateTime", ""))
    end = latest.get("endDateTime", "")
    now = datetime.now(timezone.utc).isoformat()

    if end and end > now:
        _add(
            result, id="identity_credential_expiry", category="identity",
            name="Credential Expiry",
            status="pass",
            detail=f"Valid until {end}",
            evidence=f"endDateTime={end}\nnow={now}\ncredentials_count={len(creds)}",
            command=cmd,
        )
    else:
        _add(
            result, id="identity_credential_expiry", category="identity",
            name="Credential Expiry",
            status="fail",
            detail=f"Credential EXPIRED: {end}",
            evidence=f"endDateTime={end}\nnow={now}",
            command=cmd,
        )
