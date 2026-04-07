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

    @staticmethod
    def _assert(
        result: CheckResult, fail: bool, *,
        severity: str, category: str, rtype: str,
        rg: str, name: str, title: str, detail: str,
        recommendation: str = "",
    ) -> None:
        """Record a pass or fail finding on *result*."""
        if fail:
            result.findings.append(Finding(
                severity=severity, category=category, resource_name=name,
                resource_group=rg, resource_type=rtype, title=title,
                detail=detail, recommendation=recommendation,
            ))
            result.checks_failed += 1
        else:
            result.checks_passed += 1

    def _check_storage_account(self, rg: str, name: str, result: CheckResult) -> None:
        info = self._az.json("storage", "account", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return
        props = info.get("properties", info)
        kw = dict(category="storage", rtype="Storage Account", rg=rg, name=name)

        self._assert(
            result, props.get("allowBlobPublicAccess", True),
            severity="high", title="Public blob access enabled",
            detail=f"Storage account '{name}' allows public access to blobs.",
            recommendation=f"az storage account update --name {name} --resource-group {rg} --allow-blob-public-access false",
            **kw,
        )

        https_only = info.get("enableHttpsTrafficOnly", props.get("supportsHttpsTrafficOnly", True))
        self._assert(
            result, not https_only,
            severity="high", title="HTTP traffic allowed (HTTPS not enforced)",
            detail=f"Storage account '{name}' allows non-HTTPS traffic.",
            recommendation=f"az storage account update --name {name} --resource-group {rg} --https-only true",
            **kw,
        )

        net_rules = props.get("networkRuleSet", props.get("networkAcls", {}))
        default_action = (net_rules.get("defaultAction") or "Allow").lower()
        self._assert(
            result, default_action == "allow",
            severity="medium", title="Network access not restricted",
            detail=f"Storage account '{name}' allows access from all networks.",
            recommendation=f"az storage account update --name {name} --resource-group {rg} --default-action Deny",
            **kw,
        )

        min_tls = props.get("minimumTlsVersion", "TLS1_0")
        self._assert(
            result, min_tls in ("TLS1_0", "TLS1_1"),
            severity="medium", title=f"Weak minimum TLS version ({min_tls})",
            detail=f"Storage account '{name}' allows {min_tls} connections.",
            recommendation=f"az storage account update --name {name} --resource-group {rg} --min-tls-version TLS1_2",
            **kw,
        )

    def _check_keyvault(self, rg: str, name: str, result: CheckResult) -> None:
        info = self._az.json("keyvault", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return
        props = info.get("properties", info)
        kw = dict(category="keyvault", rtype="Key Vault", rg=rg, name=name)

        self._assert(
            result, not props.get("enableRbacAuthorization", False),
            severity="high", title="RBAC authorization not enabled",
            detail=f"Key Vault '{name}' uses access policies instead of RBAC.",
            recommendation=f"az keyvault update --name {name} --resource-group {rg} --enable-rbac-authorization true",
            **kw,
        )

        soft_delete = props.get("enableSoftDelete", False)
        purge_protect = props.get("enablePurgeProtection", False)
        if not soft_delete:
            self._assert(
                result, True, severity="medium",
                title="Soft delete not enabled",
                detail=f"Key Vault '{name}' does not have soft delete enabled.",
                recommendation="Enable soft delete (default for new vaults).",
                **kw,
            )
        elif not purge_protect:
            self._assert(
                result, True, severity="low",
                title="Purge protection not enabled",
                detail=f"Key Vault '{name}' has soft delete but not purge protection.",
                recommendation=f"az keyvault update --name {name} --enable-purge-protection true",
                **kw,
            )
        else:
            result.checks_passed += 1

        net_acls = props.get("networkAcls", {})
        network_default = (net_acls.get("defaultAction") or "Allow").lower()
        public_access = props.get("publicNetworkAccess", "Enabled")
        self._assert(
            result, network_default == "allow" and public_access != "Disabled",
            severity="medium", title="Public network access not restricted",
            detail=f"Key Vault '{name}' is accessible from all networks.",
            recommendation="Restrict network access or use private endpoints.",
            **kw,
        )

    def _check_acr(self, rg: str, name: str, result: CheckResult) -> None:
        info = self._az.json("acr", "show", "--name", name, "--resource-group", rg)
        if not isinstance(info, dict):
            return
        kw = dict(category="acr", rtype="Container Registry", rg=rg, name=name)

        self._assert(
            result, info.get("adminUserEnabled", False),
            severity="medium", title="Admin user enabled",
            detail=f"Container Registry '{name}' has the admin user enabled.",
            recommendation=f"az acr update --name {name} --admin-enabled false",
            **kw,
        )
        self._assert(
            result, info.get("publicNetworkAccess", "Enabled") == "Enabled",
            severity="low", title="Public network access enabled",
            detail=f"Container Registry '{name}' is accessible from the public internet.",
            recommendation="Consider restricting via firewall rules or private endpoints.",
            **kw,
        )

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
