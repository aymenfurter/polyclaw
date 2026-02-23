"""Misconfiguration checker -- security and best-practice audits."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..cloud.azure import AzureCLI

logger = logging.getLogger(__name__)


@dataclass
class Finding:
    severity: Literal["critical", "high", "medium", "low", "info"] = "medium"
    category: str = ""
    resource_name: str = ""
    resource_group: str = ""
    resource_type: str = ""
    title: str = ""
    detail: str = ""
    recommendation: str = ""


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    resources_scanned: int = 0
    checks_passed: int = 0
    checks_failed: int = 0

    @property
    def has_critical(self) -> bool:
        return any(f.severity == "critical" for f in self.findings)

    @property
    def has_high(self) -> bool:
        return any(f.severity == "high" for f in self.findings)


class MisconfigChecker:
    """Run security misconfiguration checks against Azure resources."""

    def __init__(self, az: AzureCLI) -> None:
        self._az = az

    def check_all(self, resource_groups: list[str]) -> CheckResult:
        result = CheckResult()
        for rg in resource_groups:
            resources = self._az.json("resource", "list", "--resource-group", rg)
            if not isinstance(resources, list):
                continue
            for r in resources:
                rtype = (r.get("type") or "").lower()
                rname = r.get("name", "")
                result.resources_scanned += 1
                if "microsoft.storage/storageaccounts" in rtype:
                    self._check_storage_account(rg, rname, result)
                elif "microsoft.keyvault/vaults" in rtype:
                    self._check_keyvault(rg, rname, result)
                elif "microsoft.containerregistry/registries" in rtype:
                    self._check_acr(rg, rname, result)
        return result

    def _check_storage_account(self, rg: str, name: str, result: CheckResult) -> None:
        info = self._az.json("storage", "account", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return
        props = info.get("properties", info)

        if props.get("allowBlobPublicAccess", True):
            result.findings.append(Finding(
                severity="high", category="storage", resource_name=name, resource_group=rg,
                resource_type="Storage Account", title="Public blob access enabled",
                detail=f"Storage account '{name}' allows public access to blobs.",
                recommendation=f"az storage account update --name {name} --resource-group {rg} --allow-blob-public-access false",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

        https_only = info.get("enableHttpsTrafficOnly", props.get("supportsHttpsTrafficOnly", True))
        if not https_only:
            result.findings.append(Finding(
                severity="high", category="storage", resource_name=name, resource_group=rg,
                resource_type="Storage Account", title="HTTP traffic allowed (HTTPS not enforced)",
                detail=f"Storage account '{name}' allows non-HTTPS traffic.",
                recommendation=f"az storage account update --name {name} --resource-group {rg} --https-only true",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

        net_rules = props.get("networkRuleSet", props.get("networkAcls", {}))
        default_action = (net_rules.get("defaultAction") or "Allow").lower()
        if default_action == "allow":
            result.findings.append(Finding(
                severity="medium", category="storage", resource_name=name, resource_group=rg,
                resource_type="Storage Account", title="Network access not restricted",
                detail=f"Storage account '{name}' allows access from all networks.",
                recommendation=f"az storage account update --name {name} --resource-group {rg} --default-action Deny",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

        min_tls = props.get("minimumTlsVersion", "TLS1_0")
        if min_tls in ("TLS1_0", "TLS1_1"):
            result.findings.append(Finding(
                severity="medium", category="storage", resource_name=name, resource_group=rg,
                resource_type="Storage Account", title=f"Weak minimum TLS version ({min_tls})",
                detail=f"Storage account '{name}' allows {min_tls} connections.",
                recommendation=f"az storage account update --name {name} --resource-group {rg} --min-tls-version TLS1_2",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

    def _check_keyvault(self, rg: str, name: str, result: CheckResult) -> None:
        info = self._az.json("keyvault", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return
        props = info.get("properties", info)

        if not props.get("enableRbacAuthorization", False):
            result.findings.append(Finding(
                severity="high", category="keyvault", resource_name=name, resource_group=rg,
                resource_type="Key Vault", title="RBAC authorization not enabled",
                detail=f"Key Vault '{name}' uses access policies instead of RBAC.",
                recommendation=f"az keyvault update --name {name} --resource-group {rg} --enable-rbac-authorization true",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

        soft_delete = props.get("enableSoftDelete", False)
        purge_protect = props.get("enablePurgeProtection", False)
        if not soft_delete:
            result.findings.append(Finding(
                severity="medium", category="keyvault", resource_name=name, resource_group=rg,
                resource_type="Key Vault", title="Soft delete not enabled",
                detail=f"Key Vault '{name}' does not have soft delete enabled.",
                recommendation="Enable soft delete (default for new vaults).",
            ))
            result.checks_failed += 1
        elif not purge_protect:
            result.findings.append(Finding(
                severity="low", category="keyvault", resource_name=name, resource_group=rg,
                resource_type="Key Vault", title="Purge protection not enabled",
                detail=f"Key Vault '{name}' has soft delete but not purge protection.",
                recommendation=f"az keyvault update --name {name} --enable-purge-protection true",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

        net_acls = props.get("networkAcls", {})
        network_default = (net_acls.get("defaultAction") or "Allow").lower()
        public_access = props.get("publicNetworkAccess", "Enabled")
        if network_default == "allow" and public_access != "Disabled":
            result.findings.append(Finding(
                severity="medium", category="keyvault", resource_name=name, resource_group=rg,
                resource_type="Key Vault", title="Public network access not restricted",
                detail=f"Key Vault '{name}' is accessible from all networks.",
                recommendation="Restrict network access or use private endpoints.",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

    def _check_acr(self, rg: str, name: str, result: CheckResult) -> None:
        info = self._az.json("acr", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return

        if info.get("adminUserEnabled", False):
            result.findings.append(Finding(
                severity="medium", category="acr", resource_name=name, resource_group=rg,
                resource_type="Container Registry", title="Admin user enabled",
                detail=f"Container Registry '{name}' has the admin user enabled.",
                recommendation=f"az acr update --name {name} --admin-enabled false",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

        if info.get("publicNetworkAccess", "Enabled") == "Enabled":
            result.findings.append(Finding(
                severity="low", category="acr", resource_name=name, resource_group=rg,
                resource_type="Container Registry", title="Public network access enabled",
                detail=f"Container Registry '{name}' is accessible from the public internet.",
                recommendation="Consider restricting via firewall rules or private endpoints.",
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

    @staticmethod
    def to_dict(result: CheckResult) -> dict[str, Any]:
        return {
            "resources_scanned": result.resources_scanned,
            "checks_passed": result.checks_passed,
            "checks_failed": result.checks_failed,
            "has_critical": result.has_critical,
            "has_high": result.has_high,
            "findings": [asdict(f) for f in result.findings],
        }
