"""Azure resource network audit helpers for the network-info API."""

from __future__ import annotations

from typing import Any

from ...services.cloud.azure import AzureCLI
from ...state.foundry_iq_config import FoundryIQConfigStore
from ...state.sandbox_config import SandboxConfigStore

# Maps lowercased Azure resource type prefixes to audit functions.
_RESOURCE_AUDITORS: dict[str, Any] = {}  # populated after function definitions


def collect_resource_groups(
    cfg: Any,
    sandbox_store: SandboxConfigStore | None,
    foundry_iq_store: FoundryIQConfigStore | None,
) -> list[str]:
    """Gather all known resource groups from config stores."""
    rgs: set[str] = set()

    bot_rg = cfg.env.read("RESOURCE_GROUP") or ""
    if bot_rg:
        rgs.add(bot_rg)

    if sandbox_store:
        sb = sandbox_store.config
        if sb.resource_group:
            rgs.add(sb.resource_group)

    if foundry_iq_store:
        fiq = foundry_iq_store.config
        if fiq.resource_group:
            rgs.add(fiq.resource_group)

    deploy_rg = cfg.env.read("DEPLOY_RESOURCE_GROUP") or ""
    if deploy_rg:
        rgs.add(deploy_rg)

    voice_rg = cfg.env.read("VOICE_RESOURCE_GROUP") or ""
    if voice_rg:
        rgs.add(voice_rg)

    return list(rgs)


def audit_resource(
    az: AzureCLI, rg: str, name: str, rtype: str,
) -> dict[str, Any] | None:
    """Return a network audit dict for a single Azure resource."""
    rtype_lower = rtype.lower()
    for prefix, auditor in _RESOURCE_AUDITORS.items():
        if prefix in rtype_lower:
            return auditor(az=az, rg=rg, name=name)
    return None


# ------------------------------------------------------------------
# Per-resource audit functions
# ------------------------------------------------------------------


def _audit_storage(
    az: AzureCLI, rg: str, name: str,
) -> dict[str, Any] | None:
    info = az.json("storage", "account", "show", "--name", name, "--resource-group", rg)
    if not isinstance(info, dict):
        return None
    props = info.get("properties") or info
    net_rules = props.get("networkRuleSet") or props.get("networkAcls") or {}
    default_action = net_rules.get("defaultAction") or "Allow"
    ip_rules = net_rules.get("ipRules") or []
    vnet_rules = net_rules.get("virtualNetworkRules") or []
    allowed_ips = [r.get("value", r.get("ipAddressOrRange", "")) for r in ip_rules]
    allowed_vnets = [r.get("id", "") for r in vnet_rules]
    public_blob = props.get("allowBlobPublicAccess", True)
    https_only = info.get("enableHttpsTrafficOnly", props.get("supportsHttpsTrafficOnly", True))
    min_tls = props.get("minimumTlsVersion", "TLS1_0")
    private_eps = _get_private_endpoints(props)

    return {
        "name": name,
        "resource_group": rg,
        "type": "Storage Account",
        "icon": "storage",
        "public_access": default_action == "Allow",
        "default_action": default_action,
        "allowed_ips": allowed_ips,
        "allowed_vnets": allowed_vnets,
        "private_endpoints": private_eps,
        "https_only": https_only,
        "min_tls_version": min_tls,
        "extra": {
            "public_blob_access": public_blob,
        },
    }


def _audit_keyvault(
    az: AzureCLI, rg: str, name: str,
) -> dict[str, Any] | None:
    info = az.json("keyvault", "show", "--name", name, "--resource-group", rg)
    if not isinstance(info, dict):
        return None
    props = info.get("properties") or info
    net_acls = props.get("networkAcls") or {}
    default_action = net_acls.get("defaultAction") or "Allow"
    ip_rules = net_acls.get("ipRules") or []
    vnet_rules = net_acls.get("virtualNetworkRules") or []
    allowed_ips = [r.get("value", "") for r in ip_rules]
    allowed_vnets = [r.get("id", "") for r in vnet_rules]
    public_access = props.get("publicNetworkAccess", "Enabled")
    private_eps = _get_private_endpoints(props)
    rbac = props.get("enableRbacAuthorization", False)
    soft_delete = props.get("enableSoftDelete", False)
    purge_protect = props.get("enablePurgeProtection", False)

    return {
        "name": name,
        "resource_group": rg,
        "type": "Key Vault",
        "icon": "keyvault",
        "public_access": public_access != "Disabled" and default_action == "Allow",
        "default_action": default_action,
        "allowed_ips": allowed_ips,
        "allowed_vnets": allowed_vnets,
        "private_endpoints": private_eps,
        "extra": {
            "public_network_access": public_access,
            "rbac_authorization": rbac,
            "soft_delete": soft_delete,
            "purge_protection": purge_protect,
        },
    }


def _audit_cognitive(
    az: AzureCLI, rg: str, name: str,
) -> dict[str, Any] | None:
    """Audit Azure OpenAI / Cognitive Services accounts."""
    info = az.json(
        "cognitiveservices", "account", "show",
        "--name", name, "--resource-group", rg,
    )
    if not isinstance(info, dict):
        return None
    props = info.get("properties") or info
    net_acls = props.get("networkAcls") or {}
    default_action = net_acls.get("defaultAction") or "Allow"
    ip_rules = net_acls.get("ipRules") or []
    vnet_rules = net_acls.get("virtualNetworkRules") or []
    allowed_ips = [r.get("value", "") for r in ip_rules]
    allowed_vnets = [r.get("id", "") for r in vnet_rules]
    public_access = props.get("publicNetworkAccess", "Enabled")
    private_eps = _get_private_endpoints(props)
    kind = info.get("kind", "CognitiveServices")
    endpoint = (
        props.get("endpoint")
        or (props.get("endpoints") or {}).get("OpenAI Language Model Instance API", "")
    )

    label = "Azure OpenAI" if kind.lower() == "openai" else f"Cognitive Services ({kind})"

    return {
        "name": name,
        "resource_group": rg,
        "type": label,
        "icon": "ai",
        "public_access": public_access != "Disabled" and default_action == "Allow",
        "default_action": default_action,
        "allowed_ips": allowed_ips,
        "allowed_vnets": allowed_vnets,
        "private_endpoints": private_eps,
        "extra": {
            "public_network_access": public_access,
            "kind": kind,
            "endpoint": endpoint,
        },
    }


def _audit_search(
    az: AzureCLI, rg: str, name: str,
) -> dict[str, Any] | None:
    """Audit Azure AI Search service."""
    info = az.json(
        "search", "service", "show",
        "--name", name, "--resource-group", rg,
    )
    if not isinstance(info, dict):
        return None
    props = info.get("properties") or info
    public_access = props.get("publicNetworkAccess", "enabled")
    ip_rules = (props.get("networkRuleSet") or {}).get("ipRules") or []
    allowed_ips = [r.get("value", "") for r in ip_rules]
    private_eps = _get_private_endpoints(props)

    return {
        "name": name,
        "resource_group": rg,
        "type": "Azure AI Search",
        "icon": "search",
        "public_access": public_access.lower() != "disabled",
        "default_action": "Allow" if public_access.lower() != "disabled" else "Deny",
        "allowed_ips": allowed_ips,
        "allowed_vnets": [],
        "private_endpoints": private_eps,
        "extra": {
            "public_network_access": public_access,
            "sku": info.get("sku", {}).get("name", ""),
        },
    }


def _audit_acr(
    az: AzureCLI, rg: str, name: str,
) -> dict[str, Any] | None:
    info = az.json("acr", "show", "--name", name, "--resource-group", rg)
    if not isinstance(info, dict):
        return None
    public_access = info.get("publicNetworkAccess", "Enabled")
    net_rules = info.get("networkRuleSet") or {}
    default_action = net_rules.get("defaultAction") or "Allow"
    ip_rules = net_rules.get("ipRules") or []
    allowed_ips = [r.get("value", "") for r in ip_rules]
    admin_enabled = info.get("adminUserEnabled", False)

    return {
        "name": name,
        "resource_group": rg,
        "type": "Container Registry",
        "icon": "acr",
        "public_access": public_access == "Enabled",
        "default_action": default_action,
        "allowed_ips": allowed_ips,
        "allowed_vnets": [],
        "private_endpoints": [],
        "extra": {
            "admin_user_enabled": admin_enabled,
            "sku": info.get("sku", {}).get("name", ""),
        },
    }


def _audit_session_pool(rg: str, name: str, **_kw: Any) -> dict[str, Any]:
    """Audit Azure Container Apps session pool."""
    return {
        "name": name,
        "resource_group": rg,
        "type": "Session Pool",
        "icon": "sandbox",
        "public_access": True,
        "default_action": "Allow",
        "allowed_ips": [],
        "allowed_vnets": [],
        "private_endpoints": [],
        "extra": {},
    }


def _audit_acs(rg: str, name: str, **_kw: Any) -> dict[str, Any]:
    """Audit Azure Communication Services."""
    return {
        "name": name,
        "resource_group": rg,
        "type": "Communication Services",
        "icon": "communication",
        "public_access": True,
        "default_action": "Allow",
        "allowed_ips": [],
        "allowed_vnets": [],
        "private_endpoints": [],
        "extra": {},
    }


def _get_private_endpoints(props: dict[str, Any]) -> list[str]:
    """Extract private endpoint names from a resource's properties."""
    pe_conns = props.get("privateEndpointConnections", [])
    results: list[str] = []
    for pec in pe_conns:
        pe = pec.get("privateEndpoint", {})
        pe_id = pe.get("id", "")
        if pe_id:
            results.append(pe_id.rsplit("/", 1)[-1])
    return results


# Populate the dispatch table now that all audit functions are defined.
_RESOURCE_AUDITORS.update({
    "microsoft.storage/storageaccounts": _audit_storage,
    "microsoft.keyvault/vaults": _audit_keyvault,
    "microsoft.cognitiveservices/accounts": _audit_cognitive,
    "microsoft.search/searchservices": _audit_search,
    "microsoft.containerregistry/registries": _audit_acr,
    "microsoft.app/sessionpools": _audit_session_pool,
    "microsoft.communication/communicationservices": _audit_acs,
})
