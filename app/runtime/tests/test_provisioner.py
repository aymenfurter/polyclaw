"""Tests for Provisioner -- admin provisions Entra app + identity, agent creates bot service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.runtime.config.settings import cfg
from app.runtime.services.provisioner import Provisioner
from app.runtime.state.deploy_state import DeployStateStore
from app.runtime.state.infra_config import InfraConfigStore
from app.runtime.util.result import Result


@pytest.fixture()
def az() -> MagicMock:
    mock = MagicMock()
    mock.ok.return_value = (True, "ok")
    mock.json.return_value = None
    mock.update_endpoint.return_value = Result.ok("Endpoint updated")
    mock.validate_telegram_token.return_value = (True, "valid")
    mock.configure_telegram.return_value = (True, "configured")
    mock.last_stderr = ""
    return mock


@pytest.fixture()
def deployer() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def tunnel() -> MagicMock:
    mock = MagicMock()
    mock.is_active = False
    mock.url = None
    return mock


@pytest.fixture()
def store(data_dir) -> InfraConfigStore:
    return InfraConfigStore()


@pytest.fixture()
def deploy_store(data_dir) -> DeployStateStore:
    return DeployStateStore()


@pytest.fixture()
def provisioner(az, deployer, tunnel, store, deploy_store) -> Provisioner:
    return Provisioner(az, deployer, store, deploy_store, tunnel=tunnel)


class TestAppRegistration:
    """Verify _ensure_app_registration calls register_app on the deployer."""

    def test_calls_register_app(self, provisioner, deployer, data_dir):
        deployer.register_app.return_value = MagicMock(
            ok=True, steps=[], app_id="test-app-id", error=""
        )
        bc = MagicMock(resource_group="rg", location="eastus", display_name="polyclaw", bot_handle="")

        steps: list[dict] = []
        result = provisioner._ensure_app_registration(bc, steps)

        assert result is True
        deployer.register_app.assert_called_once()
        assert any(s["step"] == "app_registration" and s["status"] == "ok" for s in steps)

    def test_returns_false_on_failure(self, provisioner, deployer, data_dir):
        deployer.register_app.return_value = MagicMock(
            ok=False, steps=[], app_id="", error="App registration failed"
        )
        bc = MagicMock(resource_group="rg", location="eastus", display_name="polyclaw", bot_handle="")

        steps: list[dict] = []
        result = provisioner._ensure_app_registration(bc, steps)

        assert result is False
        assert any(s["step"] == "app_registration" and s["status"] == "failed" for s in steps)


class TestProvision:
    """Full provision flow -- registers Entra app + runtime identity, no bot service."""

    def test_skips_when_not_configured(self, provisioner, store):
        store.bot = MagicMock()
        store.bot.resource_group = ""
        store.bot.location = ""

        # bot_configured returns False when rg/location are empty
        with patch.object(type(store), "bot_configured", new_callable=lambda: property(lambda self: False)):
            steps = provisioner.provision()
            assert any(s["step"] == "bot_config" and s["status"] == "skip" for s in steps)

    def test_registers_app_and_identity(self, provisioner, deployer, data_dir):
        """Provision registers Entra app + runtime identity, no bot service."""
        deployer.register_app.return_value = MagicMock(
            ok=True, steps=[], app_id="test-app-id", error=""
        )

        steps = provisioner.provision()

        deployer.register_app.assert_called_once()
        # Bot service should NOT be created by admin provisioning
        deployer.deploy.assert_not_called()
        assert any(s["step"] == "app_registration" and s["status"] == "ok" for s in steps)


class TestRecreateEndpoint:
    """Verify recreate_endpoint creates bot service + channels (agent path)."""

    def test_creates_bot_and_channels(self, provisioner, deployer, store, data_dir):
        deployer.recreate.return_value = MagicMock(ok=True, steps=[])
        store.save_telegram(token="123456:ABC", whitelist="")

        steps = provisioner.recreate_endpoint("https://tunnel.example.com/api/messages")

        deployer.recreate.assert_called_once_with("https://tunnel.example.com/api/messages")
        assert any(s.get("step") == "bot_recreate" or s.get("step") == "telegram_channel"
                   for s in steps)
