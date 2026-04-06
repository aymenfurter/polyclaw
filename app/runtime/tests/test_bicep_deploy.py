"""Tests for the Bicep-based infrastructure deployer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.runtime.services.deployment.bicep_deployer import (
    BicepDeployer,
    BicepDeployRequest,
    BicepDeployResult,
    _BICEP_TEMPLATE,
    _ObservableSteps,
)
from app.runtime.state.deploy_state import DeployStateStore


class TestBicepDeployRequest:
    """BicepDeployRequest defaults and auto-naming."""

    def test_default_models(self) -> None:
        req = BicepDeployRequest()
        assert len(req.models) == 3
        names = [m["name"] for m in req.models]
        assert "gpt-4.1" in names
        assert "gpt-5" in names
        assert "gpt-5-mini" in names

    def test_auto_generates_base_name(self) -> None:
        req = BicepDeployRequest()
        assert req.base_name.startswith("polyclaw-")
        assert len(req.base_name) == len("polyclaw-") + 8

    def test_explicit_base_name(self) -> None:
        req = BicepDeployRequest(base_name="my-custom-name")
        assert req.base_name == "my-custom-name"

    def test_default_location(self) -> None:
        req = BicepDeployRequest()
        assert req.location == "eastus"

    def test_default_resource_group(self) -> None:
        req = BicepDeployRequest()
        assert req.resource_group == "polyclaw-rg"


class TestBicepDeployer:
    """Unit tests for BicepDeployer with mocked AzureCLI."""

    def _make_deployer(self) -> tuple[BicepDeployer, MagicMock, DeployStateStore]:
        az = MagicMock()
        az.last_stderr = ""
        store = DeployStateStore()
        deployer = BicepDeployer(az, store)
        return deployer, az, store

    def test_deploy_succeeds(self) -> None:
        deployer, az, store = self._make_deployer()

        # Mock: RG exists
        az.json.side_effect = self._az_json_router({
            ("group", "show"): {"name": "polyclaw-rg"},
            ("ad", "signed-in-user"): {"id": "user-oid-123", "userPrincipalName": "user@test.com"},
            ("ad", "sp", "create-for-rbac"): {"appId": "sp-app-id", "password": "sp-pw", "tenant": "sp-tenant"},
            ("ad", "sp", "show"): {"id": "sp-object-id"},
            ("deployment", "group"): {
                "foundryEndpoint": {"value": "https://myai.openai.azure.com/"},
                "foundryName": {"value": "myai"},
                "foundryResourceId": {"value": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/myai"},
                "deployedModels": {"value": ["gpt-4.1", "gpt-5", "gpt-5-mini"]},
                "keyVaultUrl": {"value": "https://myai-kv.vault.azure.net/"},
                "keyVaultName": {"value": "myai-kv"},
                "acsName": {"value": ""},
                "acsResourceId": {"value": ""},
            },
        })
        az.account_info.return_value = {"id": "sub-123", "user": {"name": "user@test.com"}, "name": "MySub"}

        req = BicepDeployRequest(base_name="myai", resource_group="polyclaw-rg")
        result = deployer.deploy(req)

        assert result.ok is True
        assert result.foundry_endpoint == "https://myai.openai.azure.com/"
        assert result.foundry_name == "myai"
        assert result.deployed_models == ["gpt-4.1", "gpt-5", "gpt-5-mini"]
        assert result.key_vault_url == "https://myai-kv.vault.azure.net/"
        assert result.deploy_id != ""

        # Verify deploy state was recorded
        assert len(store.summary()) >= 1

    def test_deploy_fails_on_rg_creation(self) -> None:
        deployer, az, _store = self._make_deployer()

        # Mock: RG does not exist and creation fails
        az.json.return_value = None
        az.last_stderr = "subscription not found"

        req = BicepDeployRequest(base_name="test")
        result = deployer.deploy(req)

        assert result.ok is False
        assert "Resource group creation failed" in result.error

    def test_deploy_fails_without_principal(self) -> None:
        deployer, az, _store = self._make_deployer()

        # RG exists but principal resolution fails
        def _side_effect(*args, **kwargs):
            if args and args[0] == "group":
                return {"name": "polyclaw-rg"}
            return None
        az.json.side_effect = _side_effect
        az.account_info.return_value = None

        req = BicepDeployRequest(base_name="test")
        result = deployer.deploy(req)

        assert result.ok is False
        assert "principal" in result.error.lower()

    def test_deploy_fails_on_bicep_error(self) -> None:
        deployer, az, _store = self._make_deployer()

        call_count = 0
        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if args and args[0] == "group":
                return {"name": "polyclaw-rg"}
            if args and args[0] == "ad" and "signed-in-user" in args:
                return {"id": "user-oid", "userPrincipalName": "u@t.com"}
            if args and args[0] == "ad" and "create-for-rbac" in args:
                return {"appId": "sp-id", "password": "pw", "tenant": "t"}
            if args and args[0] == "ad" and "show" in args:
                return {"id": "sp-oid"}
            # Bicep deployment fails
            return None

        az.json.side_effect = _side_effect
        az.account_info.return_value = {"id": "sub-1", "user": {"name": "u@t.com"}, "name": "MySub"}
        az.last_stderr = "InvalidTemplate"

        req = BicepDeployRequest(base_name="test")
        result = deployer.deploy(req)

        assert result.ok is False
        assert "Bicep deployment failed" in result.error

    def test_status_returns_env_values(self) -> None:
        deployer, _az, _store = self._make_deployer()
        status = deployer.status()
        assert "deployed" in status
        assert "foundry_endpoint" in status

    def test_decommission_no_rg(self) -> None:
        deployer, _az, _store = self._make_deployer()
        with patch("app.runtime.services.deployment.bicep_deployer.cfg") as mock_cfg:
            mock_cfg.env.read.return_value = ""
            steps = deployer.decommission("")
        assert steps[0]["status"] == "skip"

    def test_decommission_deletes_rg(self) -> None:
        deployer, az, _store = self._make_deployer()
        az.ok.return_value = (True, "")

        with patch("app.runtime.services.deployment.bicep_deployer.cfg") as mock_cfg:
            mock_cfg.env.read.return_value = ""
            mock_cfg.write_env = MagicMock()
            steps = deployer.decommission("polyclaw-rg")

        assert any(s["step"] == "delete_resource_group" and s["status"] == "ok" for s in steps)

    def test_ensure_runtime_sp_creates_new(self) -> None:
        """First deploy must create a runtime SP."""
        deployer, az, _store = self._make_deployer()

        with patch("app.runtime.services.deployment.bicep_deployer.cfg") as mock_cfg:
            mock_cfg.env.read.return_value = ""

            az.account_info.return_value = {"id": "sub-123"}
            az.json.side_effect = self._az_json_router({
                ("ad", "sp", "create-for-rbac"): {
                    "appId": "new-sp-id", "password": "new-sp-pw", "tenant": "my-tenant",
                },
                ("ad", "sp", "show"): {"id": "sp-object-id-123"},
            })

            req = BicepDeployRequest(base_name="test", resource_group="rg")
            result = deployer._ensure_runtime_sp(req, [])

        assert result is not None
        assert result["app_id"] == "new-sp-id"
        assert result["password"] == "new-sp-pw"
        assert result["tenant"] == "my-tenant"
        assert result["object_id"] == "sp-object-id-123"

    def test_ensure_runtime_sp_reuses_existing(self) -> None:
        """Existing valid SP must be reused (no new create-for-rbac)."""
        deployer, az, _store = self._make_deployer()

        with patch("app.runtime.services.deployment.bicep_deployer.cfg") as mock_cfg:
            mock_cfg.env.read.side_effect = lambda k: {
                "RUNTIME_SP_APP_ID": "existing-id",
                "RUNTIME_SP_PASSWORD": "existing-pw",
                "RUNTIME_SP_TENANT": "existing-tenant",
            }.get(k, "")

            az.json.side_effect = self._az_json_router({
                ("ad", "sp", "show"): {"id": "existing-oid"},
            })

            req = BicepDeployRequest(base_name="test", resource_group="rg")
            result = deployer._ensure_runtime_sp(req, [])

        assert result is not None
        assert result["app_id"] == "existing-id"
        assert result["object_id"] == "existing-oid"
        # create-for-rbac should NOT have been called
        for call in az.json.call_args_list:
            assert "create-for-rbac" not in call[0]

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _az_json_router(routes: dict) -> callable:
        """Create a side_effect function that routes az.json calls."""
        def _route(*args, **kwargs):
            for key, value in routes.items():
                if all(k in args for k in key):
                    return value
            return None
        return _route


class TestBicepTemplate:
    """Verify the Bicep template file exists and is syntactically valid."""

    def test_template_exists(self) -> None:
        assert _BICEP_TEMPLATE.exists(), f"Bicep template not found at {_BICEP_TEMPLATE}"

    def test_template_has_required_params(self) -> None:
        content = _BICEP_TEMPLATE.read_text()
        assert "param baseName" in content
        assert "param location" in content
        assert "param principalId" in content
        assert "param models" in content

    def test_template_has_required_outputs(self) -> None:
        content = _BICEP_TEMPLATE.read_text()
        assert "output foundryEndpoint" in content
        assert "output foundryName" in content
        assert "output deployedModels" in content
        assert "output keyVaultUrl" in content

    def test_template_creates_ai_services(self) -> None:
        content = _BICEP_TEMPLATE.read_text()
        assert "Microsoft.CognitiveServices/accounts" in content
        assert "AIServices" in content

    def test_template_creates_rbac(self) -> None:
        content = _BICEP_TEMPLATE.read_text()
        assert "Microsoft.Authorization/roleAssignments" in content
        # Cognitive Services OpenAI User role ID
        assert "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd" in content


class TestBYOKProvider:
    """Tests for the BYOK provider configuration builder."""

    def test_no_provider_without_endpoint(self) -> None:
        from app.runtime.agent.byok import build_provider_config

        with patch("app.runtime.agent.byok.cfg") as mock_cfg:
            mock_cfg.foundry_endpoint = ""
            result = build_provider_config()
        assert result is None

    def test_session_overrides_empty_without_endpoint(self) -> None:
        from app.runtime.agent.byok import build_session_overrides

        with patch("app.runtime.agent.byok.cfg") as mock_cfg:
            mock_cfg.foundry_endpoint = ""
            result = build_session_overrides()
        assert result == {}

    @patch("app.runtime.agent.byok.get_bearer_token")
    def test_provider_config_with_endpoint(self, mock_token: MagicMock) -> None:
        from app.runtime.agent.byok import build_provider_config

        mock_token.return_value = "test-token-123"
        with patch("app.runtime.agent.byok.cfg") as mock_cfg:
            mock_cfg.foundry_endpoint = "https://myai.openai.azure.com/"
            result = build_provider_config()

        assert result is not None
        assert result["type"] == "azure"
        assert result["base_url"] == "https://myai.openai.azure.com"
        assert result["bearer_token"] == "test-token-123"
        assert "api_version" in result["azure"]

    @patch("app.runtime.agent.byok.get_bearer_token")
    def test_session_overrides_with_endpoint(self, mock_token: MagicMock) -> None:
        from app.runtime.agent.byok import build_session_overrides

        mock_token.return_value = "test-token-456"
        with patch("app.runtime.agent.byok.cfg") as mock_cfg:
            mock_cfg.foundry_endpoint = "https://myai.openai.azure.com/"
            mock_cfg.copilot_model = "gpt-4.1"
            result = build_session_overrides()

        assert "provider" in result
        assert result["model"] == "gpt-4.1"

    @patch("app.runtime.agent.byok.get_bearer_token")
    def test_provider_returns_none_without_token(self, mock_token: MagicMock) -> None:
        from app.runtime.agent.byok import build_provider_config

        mock_token.return_value = ""
        with patch("app.runtime.agent.byok.cfg") as mock_cfg:
            mock_cfg.foundry_endpoint = "https://myai.openai.azure.com/"
            result = build_provider_config()

        assert result is None


class TestModelPresets:
    """Verify the simplified Foundry model presets."""

    def test_foundry_models_exist(self) -> None:
        from app.runtime.state.guardrails.risk import _MODEL_TIERS

        assert "gpt-4.1" in _MODEL_TIERS
        assert "gpt-5" in _MODEL_TIERS
        assert "gpt-5-mini" in _MODEL_TIERS

    def test_github_models_removed(self) -> None:
        from app.runtime.state.guardrails.risk import _MODEL_TIERS

        assert "claude-sonnet-4.6" not in _MODEL_TIERS
        assert "claude-opus-4.6" not in _MODEL_TIERS
        assert "gpt-5.3-codex" not in _MODEL_TIERS
        assert "gemini-3-pro-preview" not in _MODEL_TIERS

    def test_tier_assignment(self) -> None:
        from app.runtime.state.guardrails.risk import get_model_tier

        assert get_model_tier("gpt-5") == 1
        assert get_model_tier("gpt-4.1") == 2
        assert get_model_tier("gpt-5-mini") == 3

    def test_unknown_model_defaults_to_restrictive(self) -> None:
        from app.runtime.state.guardrails.risk import get_model_tier

        assert get_model_tier("unknown-model") == 3

    def test_list_model_tiers(self) -> None:
        from app.runtime.state.guardrails.risk import list_model_tiers

        tiers = list_model_tiers()
        assert len(tiers) == 3
        names = [t["model"] for t in tiers]
        assert "gpt-4.1" in names
        assert "gpt-5" in names
        assert "gpt-5-mini" in names


class TestSettingsFoundry:
    """Verify Foundry settings are loaded from env."""

    def test_foundry_settings_default_empty(self) -> None:
        from app.runtime.config.settings import Settings

        s = Settings()
        assert s.foundry_endpoint == ""
        assert s.foundry_name == ""
        assert s.foundry_resource_group == ""

    def test_default_model_is_gpt41(self) -> None:
        from app.runtime.config.settings import Settings

        s = Settings()
        assert s.copilot_model == "gpt-4.1"

    def test_default_memory_model_is_gpt41(self) -> None:
        from app.runtime.config.settings import Settings

        s = Settings()
        assert s.memory_model == "gpt-4.1"


class TestObservableSteps:
    """Tests for the _ObservableSteps callback list."""

    def test_callback_fires_on_append(self) -> None:
        received: list[dict] = []
        steps = _ObservableSteps(lambda s: received.append(s))
        steps.append({"step": "a", "status": "ok"})
        steps.append({"step": "b", "status": "failed"})
        assert len(received) == 2
        assert received[0]["step"] == "a"
        assert received[1]["step"] == "b"
        assert list(steps) == received

    def test_no_callback(self) -> None:
        steps = _ObservableSteps(None)
        steps.append({"step": "a", "status": "ok"})
        assert len(steps) == 1

    def test_callback_exception_does_not_abort(self) -> None:
        def bad_cb(_: dict) -> None:
            raise RuntimeError("boom")

        steps = _ObservableSteps(bad_cb)
        steps.append({"step": "a", "status": "ok"})
        assert len(steps) == 1

    def test_deploy_with_on_step_callback(self) -> None:
        """deploy() should invoke on_step for each step."""
        az = MagicMock()
        az.last_stderr = ""
        az.json.return_value = None  # RG creation fails
        store = DeployStateStore()
        deployer = BicepDeployer(az, store)

        received: list[dict] = []
        req = BicepDeployRequest(base_name="test")
        result = deployer.deploy(req, on_step=received.append)

        assert result.ok is False
        # Steps should have been recorded via the callback
        assert len(received) >= 1
        # The steps list on result should match
        assert list(result.steps) == received
