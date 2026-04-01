"""Tests for PrerequisitesRoutes helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.runtime.config.settings import cfg
from app.runtime.server.setup.prerequisites import PrerequisitesRoutes
from app.runtime.state.deploy_state import DeploymentRecord, DeployStateStore
from app.runtime.state.infra_config import InfraConfigStore
from app.runtime.util.result import Result


def _make_routes(
    tmp_path: Path,
    deploy_store: DeployStateStore | None = None,
    az: MagicMock | None = None,
) -> PrerequisitesRoutes:
    az = az or MagicMock()
    store = InfraConfigStore(path=tmp_path / "infra.json")
    return PrerequisitesRoutes(az, store, deploy_store=deploy_store)


class TestLinkExistingKeyvault:
    def test_links_kv_to_current_deployment(self, tmp_path: Path) -> None:
        ds = DeployStateStore(path=tmp_path / "deploys.json")
        rec = DeploymentRecord.new("local", deploy_id="aaaa1111")
        ds.register(rec)

        cfg.write_env(KEY_VAULT_NAME="polyclaw-kv-abc", KEY_VAULT_RG="polyclaw-prereq-rg")
        routes = _make_routes(tmp_path, deploy_store=ds)
        routes._link_existing_keyvault()

        updated = ds.get("aaaa1111")
        assert updated is not None
        assert len(updated.resources) == 1
        assert updated.resources[0].resource_name == "polyclaw-kv-abc"
        assert updated.resources[0].resource_type == "keyvault"
        assert "polyclaw-prereq-rg" in updated.resource_groups

    def test_idempotent(self, tmp_path: Path) -> None:
        ds = DeployStateStore(path=tmp_path / "deploys.json")
        rec = DeploymentRecord.new("local", deploy_id="bbbb2222")
        ds.register(rec)

        cfg.write_env(KEY_VAULT_NAME="polyclaw-kv-xyz", KEY_VAULT_RG="polyclaw-prereq-rg")
        routes = _make_routes(tmp_path, deploy_store=ds)
        routes._link_existing_keyvault()
        routes._link_existing_keyvault()  # second call should be no-op

        updated = ds.get("bbbb2222")
        assert len(updated.resources) == 1

    def test_noop_without_deploy_store(self, tmp_path: Path) -> None:
        routes = _make_routes(tmp_path, deploy_store=None)
        cfg.write_env(KEY_VAULT_NAME="polyclaw-kv-nope", KEY_VAULT_RG="rg1")
        routes._link_existing_keyvault()  # should not raise

    def test_noop_without_active_deployment(self, tmp_path: Path) -> None:
        ds = DeployStateStore(path=tmp_path / "deploys.json")
        routes = _make_routes(tmp_path, deploy_store=ds)
        cfg.write_env(KEY_VAULT_NAME="polyclaw-kv-orphan", KEY_VAULT_RG="rg1")
        routes._link_existing_keyvault()  # should not raise

    def test_noop_without_kv_name(self, tmp_path: Path) -> None:
        # Overwrite any leftover env values from earlier tests
        env_path = cfg.env.path
        env_path.write_text("")

        ds = DeployStateStore(path=tmp_path / "deploys.json")
        rec = DeploymentRecord.new("local", deploy_id="cccc3333")
        ds.register(rec)

        routes = _make_routes(tmp_path, deploy_store=ds)
        routes._link_existing_keyvault()

        updated = ds.get("cccc3333")
        assert len(updated.resources) == 0


class TestRegisterKeyvaultProvider:
    async def test_success_appends_ok_step(self, tmp_path: Path) -> None:
        az = MagicMock()
        az.ok.return_value = Result.ok("Registered")
        routes = _make_routes(tmp_path, az=az)

        steps: list[dict] = []
        result = await routes._register_keyvault_provider(steps)

        assert result is True
        assert len(steps) == 1
        assert steps[0]["step"] == "provider_registration"
        assert steps[0]["status"] == "ok"
        assert "Microsoft.KeyVault" in steps[0]["detail"]
        az.ok.assert_called_once_with(
            "provider", "register", "--namespace", "Microsoft.KeyVault", "--wait",
        )

    async def test_failure_appends_failed_step(self, tmp_path: Path) -> None:
        az = MagicMock()
        az.ok.return_value = Result.fail("Subscription not found")
        routes = _make_routes(tmp_path, az=az)

        steps: list[dict] = []
        result = await routes._register_keyvault_provider(steps)

        assert result is False
        assert len(steps) == 1
        assert steps[0]["step"] == "provider_registration"
        assert steps[0]["status"] == "failed"
        assert "Subscription not found" in steps[0]["detail"]
