"""End-to-end Azure deployment test.

Provisions REAL Azure resources via the Bicep template, verifies they appear
in the subscription, exercises enable/disable flows through the Python API
layer, and tears everything down at the end.

Usage:
    pytest app/runtime/tests/test_e2e_bicep_azure.py --run-slow -s -v

Requires:
    - ``az login`` (active session)
    - Sufficient Azure quota in eastus for CognitiveServices, Search, etc.
    - The test creates its own resource group and deletes it on teardown.

Cost: deploys S0 Cognitive Services (Foundry, Content Safety), basic
Search, Log Analytics, App Insights.  Teardown deletes the RG which
cascade-deletes everything.  Typical wall-clock time: 5-8 minutes.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time

import pytest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RG = "polyclaw-e2e-test-rg"
_LOCATION = "eastus"
_BASE_NAME = "pclawe2etest"
_TIMEOUT_AZ = 120  # seconds for az CLI calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _az(*args: str, timeout: int = _TIMEOUT_AZ) -> subprocess.CompletedProcess[str]:
    """Run an ``az`` CLI command and return the result."""
    return subprocess.run(
        ["az", *args, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _az_json(*args: str, timeout: int = _TIMEOUT_AZ) -> dict | list | None:
    """Run ``az`` and parse JSON output.  Return None on failure."""
    r = _az(*args, timeout=timeout)
    if r.returncode != 0:
        logger.warning("az %s failed (rc=%d): %s", args[0], r.returncode, r.stderr[:300])
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _wait_for_resource(resource_type: str, name: str, rg: str = _RG, timeout: int = 180) -> bool:
    """Poll until a resource appears in the resource group."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resources = _az_json(
            "resource", "list",
            "--resource-group", rg,
            "--resource-type", resource_type,
            "--query", f"[?name=='{name}']",
        )
        if resources and len(resources) > 0:
            return True
        time.sleep(5)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _az_available() -> None:
    """Skip if Azure CLI is not logged in."""
    r = _az("account", "show", timeout=15)
    if r.returncode != 0:
        pytest.skip("Azure CLI not logged in")


@pytest.fixture(scope="module")
def deployer(_az_available):
    """Create a BicepDeployer wired to a real AzureCLI."""
    from app.runtime.services.cloud.azure import AzureCLI
    from app.runtime.services.deployment.bicep_deployer import BicepDeployer
    from app.runtime.state.deploy_state import DeployStateStore

    az = AzureCLI()
    store = DeployStateStore()
    return BicepDeployer(az, store)


@pytest.fixture(scope="module")
def az_cli(_az_available):
    """Real AzureCLI instance."""
    from app.runtime.services.cloud.azure import AzureCLI
    return AzureCLI()


@pytest.fixture(scope="module", autouse=True)
def _cleanup_rg(_az_available):
    """Ensure the test resource group is deleted after all tests."""
    yield
    logger.info("Tearing down resource group %s ...", _RG)
    _az(
        "group", "delete",
        "--name", _RG,
        "--yes",
        "--no-wait",
        timeout=30,
    )
    logger.info("Resource group %s deletion initiated (no-wait).", _RG)


# ---------------------------------------------------------------------------
# Tests -- ordered, each builds on the previous
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestE2EBicepDeploy:
    """Full deployment lifecycle against real Azure."""

    # -- Phase 1: Foundry + Key Vault (core deploy) -----------------------

    def test_01_deploy_foundry_and_keyvault(self, deployer) -> None:
        """Deploy Foundry AI Services with 3 models and Key Vault."""
        from app.runtime.services.deployment.bicep_deployer import BicepDeployRequest

        req = BicepDeployRequest(
            resource_group=_RG,
            location=_LOCATION,
            base_name=_BASE_NAME,
            deploy_foundry=True,
            deploy_key_vault=True,
            deploy_acs=False,
            deploy_content_safety=False,
            deploy_search=False,
            deploy_embedding_aoai=False,
            deploy_monitoring=False,
            deploy_session_pool=False,
        )

        result = deployer.deploy(req)

        logger.info("Foundry deploy result: ok=%s steps=%s", result.ok, result.steps)
        if not result.ok:
            logger.error("Deploy error: %s", result.error)
            for s in result.steps:
                logger.error("  step: %s", s)

        assert result.ok, f"Foundry deploy failed: {result.error}"
        assert result.foundry_endpoint, "No Foundry endpoint returned"
        assert result.foundry_name == _BASE_NAME
        assert len(result.deployed_models) == 3
        assert "gpt-4.1" in result.deployed_models
        assert "gpt-5" in result.deployed_models
        assert "gpt-5-mini" in result.deployed_models
        assert result.key_vault_url, "No Key Vault URL returned"
        assert result.key_vault_name == f"{_BASE_NAME}-kv"

    # -- Phase 2: verify Azure resources exist ----------------------------

    def test_02_foundry_resource_exists(self) -> None:
        """AI Services resource exists in the resource group."""
        resources = _az_json(
            "resource", "list",
            "--resource-group", _RG,
            "--resource-type", "Microsoft.CognitiveServices/accounts",
            "--query", f"[?name=='{_BASE_NAME}']",
        )
        assert resources and len(resources) == 1, (
            f"Expected 1 AI Services resource '{_BASE_NAME}', got: {resources}"
        )
        assert resources[0]["kind"] == "AIServices"

    def test_03_keyvault_resource_exists(self) -> None:
        """Key Vault exists in the resource group."""
        kv_name = f"{_BASE_NAME}-kv"
        resources = _az_json(
            "resource", "list",
            "--resource-group", _RG,
            "--resource-type", "Microsoft.KeyVault/vaults",
            "--query", f"[?name=='{kv_name}']",
        )
        assert resources and len(resources) == 1, (
            f"Expected 1 Key Vault '{kv_name}', got: {resources}"
        )

    def test_04_model_deployments_exist(self) -> None:
        """All three model deployments exist on the AI Services resource."""
        deployments = _az_json(
            "cognitiveservices", "account", "deployment", "list",
            "--name", _BASE_NAME,
            "--resource-group", _RG,
        )
        assert deployments is not None, "Failed to list model deployments"
        names = [d.get("name") for d in deployments]
        logger.info("Model deployments found: %s", names)
        assert "gpt-4.1" in names
        assert "gpt-5" in names
        assert "gpt-5-mini" in names

    def test_05_rbac_assignment_exists(self) -> None:
        """At least one RBAC role assignment exists on the Foundry resource."""
        resource_id = _az_json(
            "cognitiveservices", "account", "show",
            "--name", _BASE_NAME,
            "--resource-group", _RG,
            "--query", "id",
        )
        assert resource_id, "AI Services resource not found"

        roles = _az_json(
            "role", "assignment", "list",
            "--scope", resource_id,
            "--query", "[?roleDefinitionName=='Cognitive Services OpenAI User']",
        )
        assert roles and len(roles) >= 1, "Missing RBAC assignment"

    # -- Phase 3: enable Content Safety via incremental Bicep deploy ------

    def test_06_deploy_content_safety(self, deployer) -> None:
        """Deploy Content Safety into the same RG via Bicep."""
        from app.runtime.services.deployment.bicep_deployer import BicepDeployRequest

        req = BicepDeployRequest(
            resource_group=_RG,
            location=_LOCATION,
            base_name=_BASE_NAME,
            deploy_foundry=True,          # re-declare (idempotent)
            deploy_key_vault=True,         # re-declare (idempotent)
            deploy_content_safety=True,    # new
            deploy_search=False,
            deploy_embedding_aoai=False,
            deploy_monitoring=False,
            deploy_session_pool=False,
        )

        result = deployer.deploy(req)

        logger.info("Content Safety deploy: ok=%s", result.ok)
        assert result.ok, f"Content Safety deploy failed: {result.error}"
        assert result.content_safety_endpoint, "No CS endpoint"
        assert result.content_safety_name == f"{_BASE_NAME}-content-safety"

        # Original resources should still be intact
        assert result.foundry_endpoint, "Foundry lost after incremental deploy"
        assert result.key_vault_url, "KV lost after incremental deploy"

    def test_07_content_safety_resource_exists(self) -> None:
        """Content Safety resource appeared in the RG."""
        cs_name = f"{_BASE_NAME}-content-safety"
        resources = _az_json(
            "resource", "list",
            "--resource-group", _RG,
            "--resource-type", "Microsoft.CognitiveServices/accounts",
            "--query", f"[?name=='{cs_name}']",
        )
        assert resources and len(resources) == 1, (
            f"Expected Content Safety resource '{cs_name}', got: {resources}"
        )
        assert resources[0]["kind"] == "ContentSafety"

    # -- Phase 4: enable Monitoring via incremental Bicep deploy ----------

    def test_08_deploy_monitoring(self, deployer) -> None:
        """Deploy Log Analytics + App Insights into the same RG."""
        from app.runtime.services.deployment.bicep_deployer import BicepDeployRequest

        req = BicepDeployRequest(
            resource_group=_RG,
            location=_LOCATION,
            base_name=_BASE_NAME,
            deploy_foundry=True,
            deploy_key_vault=True,
            deploy_content_safety=True,
            deploy_monitoring=True,        # new
            deploy_search=False,
            deploy_embedding_aoai=False,
            deploy_session_pool=False,
        )

        result = deployer.deploy(req)

        logger.info("Monitoring deploy: ok=%s", result.ok)
        assert result.ok, f"Monitoring deploy failed: {result.error}"
        assert result.app_insights_connection_string, "No AppInsights connection string"
        assert result.app_insights_name == f"{_BASE_NAME}-insights"
        assert result.log_analytics_workspace_name == f"{_BASE_NAME}-logs"

    def test_09_monitoring_resources_exist(self) -> None:
        """App Insights and Log Analytics appeared in the RG."""
        # App Insights
        ai_name = f"{_BASE_NAME}-insights"
        ai = _az_json(
            "resource", "list",
            "--resource-group", _RG,
            "--resource-type", "Microsoft.Insights/components",
            "--query", f"[?name=='{ai_name}']",
        )
        assert ai and len(ai) == 1, f"Expected App Insights '{ai_name}'"

        # Log Analytics
        la_name = f"{_BASE_NAME}-logs"
        la = _az_json(
            "resource", "list",
            "--resource-group", _RG,
            "--resource-type", "Microsoft.OperationalInsights/workspaces",
            "--query", f"[?name=='{la_name}']",
        )
        assert la and len(la) == 1, f"Expected Log Analytics '{la_name}'"

    # -- Phase 5: enable Search + Embedding AOAI (Foundry IQ) ------------

    def test_10_deploy_foundry_iq(self, deployer) -> None:
        """Deploy Search + Embedding AOAI for Foundry IQ."""
        from app.runtime.services.deployment.bicep_deployer import BicepDeployRequest

        req = BicepDeployRequest(
            resource_group=_RG,
            location=_LOCATION,
            base_name=_BASE_NAME,
            deploy_foundry=True,
            deploy_key_vault=True,
            deploy_content_safety=True,
            deploy_monitoring=True,
            deploy_search=True,            # new
            deploy_embedding_aoai=True,    # new
            deploy_session_pool=False,
        )

        result = deployer.deploy(req)

        logger.info("Foundry IQ deploy: ok=%s", result.ok)
        assert result.ok, f"Foundry IQ deploy failed: {result.error}"
        assert result.search_endpoint, "No search endpoint"
        assert result.search_name == f"{_BASE_NAME}-search"
        assert result.embedding_aoai_endpoint, "No embedding AOAI endpoint"
        assert result.embedding_aoai_name == f"{_BASE_NAME}-aoai"
        assert result.embedding_deployment_name == "text-embedding-3-large"

    def test_11_search_resource_exists(self) -> None:
        """Azure AI Search resource appeared."""
        search_name = f"{_BASE_NAME}-search"
        resources = _az_json(
            "resource", "list",
            "--resource-group", _RG,
            "--resource-type", "Microsoft.Search/searchServices",
            "--query", f"[?name=='{search_name}']",
        )
        assert resources and len(resources) == 1

    def test_12_embedding_aoai_exists(self) -> None:
        """Embedding Azure OpenAI resource appeared with model deployment."""
        aoai_name = f"{_BASE_NAME}-aoai"
        resources = _az_json(
            "resource", "list",
            "--resource-group", _RG,
            "--resource-type", "Microsoft.CognitiveServices/accounts",
            "--query", f"[?name=='{aoai_name}']",
        )
        assert resources and len(resources) == 1
        assert resources[0]["kind"] == "OpenAI"

        # Check model deployment
        deployments = _az_json(
            "cognitiveservices", "account", "deployment", "list",
            "--name", aoai_name,
            "--resource-group", _RG,
        )
        assert deployments is not None
        names = [d.get("name") for d in deployments]
        assert "text-embedding-3-large" in names

    # -- Phase 6: full resource inventory ---------------------------------

    def test_13_full_resource_inventory(self) -> None:
        """All expected resources exist in the RG."""
        resources = _az_json(
            "resource", "list",
            "--resource-group", _RG,
            "--query", "[].{name: name, type: type, kind: kind}",
        )
        assert resources is not None

        names = {r["name"] for r in resources}
        logger.info("Resources in %s: %s", _RG, sorted(names))

        expected = {
            _BASE_NAME,                          # AI Services (Foundry)
            f"{_BASE_NAME}-kv",                  # Key Vault
            f"{_BASE_NAME}-content-safety",      # Content Safety
            f"{_BASE_NAME}-logs",                # Log Analytics
            f"{_BASE_NAME}-insights",            # App Insights
            f"{_BASE_NAME}-search",              # AI Search
            f"{_BASE_NAME}-aoai",                # Embedding AOAI
        }
        missing = expected - names
        assert not missing, f"Missing resources: {missing}"

    # -- Phase 7: idempotency -- re-deploy same config --------------------

    def test_14_idempotent_redeploy(self, deployer) -> None:
        """Re-deploying the same config succeeds without errors."""
        from app.runtime.services.deployment.bicep_deployer import BicepDeployRequest

        req = BicepDeployRequest(
            resource_group=_RG,
            location=_LOCATION,
            base_name=_BASE_NAME,
            deploy_foundry=True,
            deploy_key_vault=True,
            deploy_content_safety=True,
            deploy_monitoring=True,
            deploy_search=True,
            deploy_embedding_aoai=True,
            deploy_session_pool=False,
        )

        result = deployer.deploy(req)

        assert result.ok, f"Idempotent redeploy failed: {result.error}"
        # All outputs should still be present
        assert result.foundry_endpoint
        assert result.key_vault_url
        assert result.content_safety_endpoint
        assert result.app_insights_connection_string
        assert result.search_endpoint
        assert result.embedding_aoai_endpoint

    # -- Phase 8: disable a service (remove Content Safety) ---------------

    def test_15_deploy_without_content_safety(self, deployer) -> None:
        """Deploy with Content Safety disabled -- resource should remain
        (Bicep incremental mode does not delete resources it does not
        manage), but the outputs should reflect the disabled flag."""
        from app.runtime.services.deployment.bicep_deployer import BicepDeployRequest

        req = BicepDeployRequest(
            resource_group=_RG,
            location=_LOCATION,
            base_name=_BASE_NAME,
            deploy_foundry=True,
            deploy_key_vault=True,
            deploy_content_safety=False,   # disabled
            deploy_monitoring=True,
            deploy_search=True,
            deploy_embedding_aoai=True,
            deploy_session_pool=False,
        )

        result = deployer.deploy(req)

        assert result.ok, f"Deploy-without-CS failed: {result.error}"
        # CS outputs should be empty (conditional block not evaluated)
        assert result.content_safety_endpoint == ""
        assert result.content_safety_name == ""
        # Other resources still intact
        assert result.foundry_endpoint
        assert result.key_vault_url
        assert result.app_insights_connection_string

    # -- Phase 9: decommission (delete resource group) --------------------

    def test_16_decommission(self, deployer) -> None:
        """Decommission deletes the resource group."""
        steps = deployer.decommission(_RG)
        logger.info("Decommission steps: %s", steps)

        ok_steps = [s for s in steps if s["status"] == "ok"]
        assert len(ok_steps) >= 1, f"Decommission had no OK steps: {steps}"

        rg_step = next(
            (s for s in steps if s["step"] == "delete_resource_group"), None,
        )
        assert rg_step is not None
        assert rg_step["status"] == "ok"

    def test_17_resource_group_deleted(self) -> None:
        """After decommission, the RG should be gone (or deleting)."""
        # Give Azure a moment to start the deletion
        time.sleep(10)

        rg = _az_json("group", "show", "--name", _RG, timeout=30)
        if rg is not None:
            # It may still be 'Deleting'
            state = rg.get("properties", {}).get("provisioningState", "")
            logger.info("RG %s state after decommission: %s", _RG, state)
            assert state in ("Deleting", "Deleted", ""), (
                f"RG in unexpected state: {state}"
            )
