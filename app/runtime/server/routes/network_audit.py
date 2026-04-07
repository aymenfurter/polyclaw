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


def _get_private_endpoints(props: dict[str, Any]) -> list[str]:
    """Extract private endpoint names from a resource's properties."""
    pe_conns = props.get("privateEndpointConnections", [])
    return [
        pec.get("privateEndpoint", {}).get("id", "").rsplit("/", 1)[-1]
        for pec in pe_conns
        if pec.get("privateEndpoint", {}).get("id")
    ]


def _parse_network(
    info: dict[str, Any],
    acl_key: str = "networkAcls",
) -> dict[str, Any]:
    """Extract common network-audit fields from a resource response."""
    props = info.get("properties") or info
    net_acls = (
        props.get(acl_key) or props.get("networkRuleSet")
        or props.get("networkAcls") or {}
    )
    default_action = net_acls.get("defaultAction") or "Allow"
    ip_rules = net_acls.get("ipRules") or []
    vnet_rules = net_acls.get("virtualNetworkRules") or []
    public_access_field = props.get("publicNetworkAccess", "Enabled")
    return {
        "default_action": default_action,
        "allowed_ips": [
            r.get("value", r.get("ipAddressOrRange", "")) for r in ip_rules
        ],
        "allowed_vnets": [r.get("id", "") for r in vnet_rules],
        "private_endpoints": _get_private_endpoints(props),
        "public_access_field": public_access_field,
        "props": props,
    }


def _base_result(
    name: str, rg: str, rtype: str, icon: str,
    public_access: bool, net: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name, "resource_group": rg,
        "type": rtype, "icon": icon,
        "public_access": public_access,
        "default_action": net["default_action"],
        "allowed_ips": net["allowed_ips"],
        "allowed_vnets": net["allowed_vnets"],
        "private_endpoints": net["private_endpoints"],
        "extra": extra or {},
    }


def _stub_result(name: str, rg: str, rtype: str, icon: str) -> dict[str, Any]:
    """Return a minimal audit dict for resources without CLI inspection."""
    return _base_result(name, rg, rtype, icon, True,
                        {"default_action": "Allow", "allowed_ips": [],
                         "allowed_vnets": [], "private_endpoints": []})


# ------------------------------------------------------------------
# Per-resource audit functions
# ------------------------------------------------------------------


def _audit_storage(az: AzureCLI, rg: str, name: str) -> dict[str, Any] | None:
    info = az.json("storage", "account", "show", "--name", name, "--resource-group", rg)
    if not isinstance(info, dict):
        return None
    net = _parse_network(info, "networkRuleSet")
    props = net["props"]
    return _base_result(name, rg, "Storage Account", "storage",
                        net["default_action"] == "Allow", net, {
                            "public_blob_access": props.get("allowBlobPublicAccess", True),
                            "https_only": info.get("enableHttpsTrafficOnly",
                                                   props.get("supportsHttpsTrafficOnly", True)),
                            "min_tls_version": props.get("minimumTlsVersion", "TLS1_0"),
                        })


def _audit_keyvault(az: AzureCLI, rg: str, name: str) -> dict[str, Any] | None:
    info = az.json("keyvault", "show", "--name", name, "--resource-group", rg)
    if not isinstance(info, dict):
        return None
    net = _parse_network(info)
    props = net["props"]
    pa = props.get("publicNetworkAccess", "Enabled")
    return _base_result(name, rg, "Key Vault", "keyvault",
                        pa != "Disabled" and net["default_action"] == "Allow", net, {
                            "public_network_access": pa,
                            "rbac_authorization": props.get("enableRbacAuthorization", False),
                            "soft_delete": props.get("enableSoftDelete", False),
                            "purge_protection": props.get("enablePurgeProtection", False),
                        })


def _audit_cognitive(az: AzureCLI, rg: str, name: str) -> dict[str, Any] | None:
    info = az.json("cognitiveservices", "account", "show",
                   "--name", name, "--resource-group", rg)
    if not isinstance(info, dict):
        return None
    net = _parse_network(info)
    props = net["props"]
    pa = props.get("publicNetworkAccess", "Enabled")
    kind = info.get("kind", "CognitiveServices")
    endpoint = (props.get("endpoint")
                or (props.get("endpoints") or {}).get(
                    "OpenAI Language Model Instance API", ""))
    label = "Azure OpenAI" if kind.lower() == "openai" else f"Cognitive Services ({kind})"
    return _base_result(name, rg, label, "ai",
                        pa != "Disabled" and net["default_action"] == "Allow", net, {
                            "public_network_access": pa, "kind": kind, "endpoint": endpoint,
                        })


def _audit_search(az: AzureCLI, rg: str, name: str) -> dict[str, Any] | None:
    info = az.json("search", "service", "show",
                   "--name", name, "--resource-group", rg)
    if not isinstance(info, dict):
        return None
    net = _parse_network(info)
    pa = net["public_access_field"]
    public = pa.lower() != "disabled" if isinstance(pa, str) else bool(pa)
    return _base_result(name, rg, "Azure AI Search", "search", public, net, {
        "public_network_access": pa,
        "sku": info.get("sku", {}).get("name", ""),
    })


def _audit_acr(az: AzureCLI, rg: str, name: str) -> dict[str, Any] | None:
    info = az.json("acr", "show", "--name", name, "--resource-group", rg)
    if not isinstance(info, dict):
        return None
    net = _parse_network(info)
    pa = info.get("publicNetworkAccess", "Enabled")
    return _base_result(name, rg, "Container Registry", "acr", pa == "Enabled", net, {
        "admin_user_enabled": info.get("adminUserEnabled", False),
        "sku": info.get("sku", {}).get("name", ""),
    })


def _audit_session_pool(rg: str, name: str, **_kw: Any) -> dict[str, Any]:
    return _stub_result(name, rg, "Session Pool", "sandbox")


def _audit_acs(rg: str, name: str, **_kw: Any) -> dict[str, Any]:
    return _stub_result(name, rg, "Communication Services", "communication")


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
