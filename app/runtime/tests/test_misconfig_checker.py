"""Tests for the MisconfigChecker."""

from __future__ import annotations

from app.runtime.services.security.misconfig_checker import CheckResult, Finding, MisconfigChecker


class _FakeAzureCLI:
    def __init__(self, responses: dict[str, list | dict | None] | None = None) -> None:
        self._responses = responses or {}
        self._calls: list[tuple] = []

    def json(self, *args: str) -> list | dict | None:
        self._calls.append(args)
        key = " ".join(args)
        return self._responses.get(key)


class TestCheckResult:
    def test_has_critical(self) -> None:
        result = CheckResult(findings=[Finding(severity="critical")])
        assert result.has_critical
        assert not result.has_high

    def test_has_high(self) -> None:
        result = CheckResult(findings=[Finding(severity="high")])
        assert result.has_high
        assert not result.has_critical

    def test_empty(self) -> None:
        result = CheckResult()
        assert not result.has_critical
        assert not result.has_high


class TestMisconfigChecker:
    def test_empty_resource_group(self) -> None:
        az = _FakeAzureCLI({"resource list --resource-group rg1": []})
        checker = MisconfigChecker(az)
        result = checker.check_all(["rg1"])
        assert result.resources_scanned == 0

    def test_storage_account_public_blob(self) -> None:
        az = _FakeAzureCLI({
            "resource list --resource-group rg1": [
                {"type": "microsoft.storage/storageaccounts", "name": "sa1"}
            ],
            "storage account show --name sa1 --resource-group rg1": {
                "properties": {
                    "allowBlobPublicAccess": True,
                    "networkRuleSet": {"defaultAction": "Allow"},
                    "minimumTlsVersion": "TLS1_0",
                },
                "enableHttpsTrafficOnly": False,
            },
        })
        checker = MisconfigChecker(az)
        result = checker.check_all(["rg1"])
        assert result.resources_scanned == 1
        assert result.checks_failed >= 3
        severities = {f.severity for f in result.findings}
        assert "high" in severities

    def test_keyvault_no_rbac(self) -> None:
        az = _FakeAzureCLI({
            "resource list --resource-group rg1": [
                {"type": "microsoft.keyvault/vaults", "name": "kv1"}
            ],
            "keyvault show --name kv1 --resource-group rg1": {
                "properties": {
                    "enableRbacAuthorization": False,
                    "enableSoftDelete": True,
                    "enablePurgeProtection": True,
                    "networkAcls": {"defaultAction": "Deny"},
                    "publicNetworkAccess": "Disabled",
                },
            },
        })
        checker = MisconfigChecker(az)
        result = checker.check_all(["rg1"])
        rbac_findings = [f for f in result.findings if "RBAC" in f.title]
        assert len(rbac_findings) == 1

    def test_acr_admin_enabled(self) -> None:
        az = _FakeAzureCLI({
            "resource list --resource-group rg1": [
                {"type": "microsoft.containerregistry/registries", "name": "acr1"}
            ],
            "acr show --name acr1 --resource-group rg1": {
                "adminUserEnabled": True,
                "publicNetworkAccess": "Enabled",
            },
        })
        checker = MisconfigChecker(az)
        result = checker.check_all(["rg1"])
        assert result.checks_failed >= 2

    def test_all_secure(self) -> None:
        az = _FakeAzureCLI({
            "resource list --resource-group rg1": [
                {"type": "microsoft.storage/storageaccounts", "name": "sa1"}
            ],
            "storage account show --name sa1 --resource-group rg1": {
                "properties": {
                    "allowBlobPublicAccess": False,
                    "networkRuleSet": {"defaultAction": "Deny"},
                    "minimumTlsVersion": "TLS1_2",
                },
                "enableHttpsTrafficOnly": True,
            },
        })
        checker = MisconfigChecker(az)
        result = checker.check_all(["rg1"])
        assert result.checks_failed == 0
        assert result.checks_passed == 4
        assert len(result.findings) == 0

    def test_to_dict(self) -> None:
        result = CheckResult(
            findings=[Finding(severity="high", title="Test")],
            resources_scanned=1,
            checks_passed=0,
            checks_failed=1,
        )
        d = MisconfigChecker.to_dict(result)
        assert d["resources_scanned"] == 1
        assert d["has_high"]
        assert len(d["findings"]) == 1

    def test_multiple_resource_groups(self) -> None:
        az = _FakeAzureCLI({
            "resource list --resource-group rg1": [
                {"type": "microsoft.storage/storageaccounts", "name": "sa1"}
            ],
            "resource list --resource-group rg2": [
                {"type": "microsoft.keyvault/vaults", "name": "kv1"}
            ],
            "storage account show --name sa1 --resource-group rg1": {
                "properties": {
                    "allowBlobPublicAccess": False,
                    "networkRuleSet": {"defaultAction": "Deny"},
                    "minimumTlsVersion": "TLS1_2",
                },
                "enableHttpsTrafficOnly": True,
            },
            "keyvault show --name kv1 --resource-group rg2": {
                "properties": {
                    "enableRbacAuthorization": True,
                    "enableSoftDelete": True,
                    "enablePurgeProtection": True,
                    "networkAcls": {"defaultAction": "Deny"},
                },
            },
        })
        checker = MisconfigChecker(az)
        result = checker.check_all(["rg1", "rg2"])
        assert result.resources_scanned == 2
        assert result.checks_failed == 0
