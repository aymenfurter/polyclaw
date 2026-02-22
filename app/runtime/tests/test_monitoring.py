"""Unit tests for monitoring -- config store, OTel helpers, and routes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.runtime.services.otel import (
    _reset_otel_state,
    agent_span,
    configure_otel,
    get_status,
    invoke_agent_span,
    is_active,
    record_event,
    set_span_attribute,
    shutdown_otel,
)
from app.runtime.state.monitoring_config import MonitoringConfig, MonitoringConfigStore
from app.runtime.util.result import Result

_FAKE_CS = (
    "InstrumentationKey=00000000-0000-0000-0000-ffffffffffff;"
    "IngestionEndpoint=https://eastus-0.in.applicationinsights.azure.com/;"
    "LiveEndpoint=https://eastus.livediagnostics.monitor.azure.com/;"
    "ApplicationId=deadbeef-1234-5678-9abc-def012345678"
)


# -----------------------------------------------------------------------
# MonitoringConfigStore
# -----------------------------------------------------------------------


class TestMonitoringConfigStore:
    """Tests for the JSON-file-backed monitoring config store."""

    def test_defaults(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        assert store.enabled is False
        assert store.connection_string == ""
        assert store.is_configured is False
        assert store.is_provisioned is False
        assert store.config.sampling_ratio == 1.0

    def test_update_and_persist(self, tmp_path: Path) -> None:
        path = tmp_path / "mon.json"
        store = MonitoringConfigStore(path=path)
        store.update(enabled=True, connection_string=_FAKE_CS, sampling_ratio=0.5)

        assert store.enabled is True
        assert store.connection_string == _FAKE_CS
        assert store.config.sampling_ratio == 0.5
        assert path.exists()

        # Reload from disk
        store2 = MonitoringConfigStore(path=path)
        assert store2.enabled is True
        assert store2.connection_string == _FAKE_CS
        assert store2.config.sampling_ratio == 0.5

    def test_is_configured(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.update(enabled=True)
        assert store.is_configured is False  # no connection string

        store.update(connection_string=_FAKE_CS)
        assert store.is_configured is True

        store.update(enabled=False)
        assert store.is_configured is False

    def test_set_provisioned_metadata(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.set_provisioned_metadata(
            app_insights_name="test-ai",
            workspace_name="test-ws",
            resource_group="test-rg",
            location="westus",
            connection_string=_FAKE_CS,
        )
        assert store.is_provisioned is True
        assert store.enabled is True
        assert store.config.app_insights_name == "test-ai"
        assert store.config.workspace_name == "test-ws"
        assert store.config.resource_group == "test-rg"
        assert store.config.location == "westus"

    def test_clear_provisioned_metadata(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.set_provisioned_metadata(
            app_insights_name="test-ai",
            workspace_name="test-ws",
            resource_group="test-rg",
            location="westus",
            connection_string=_FAKE_CS,
        )
        store.clear_provisioned_metadata()

        assert store.is_provisioned is False
        assert store.enabled is False
        assert store.connection_string == ""
        assert store.config.app_insights_name == ""

    def test_to_dict_masks_connection_string(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.update(enabled=True, connection_string=_FAKE_CS)

        d = store.to_dict()
        assert "connection_string" not in d
        assert d["connection_string_set"] is True
        assert d["connection_string_masked"]
        assert "*" in d["connection_string_masked"]
        # Should only show first 8 and last 4 chars of the ikey
        assert d["connection_string_masked"].startswith("00000000")
        assert d["connection_string_masked"].endswith("ffff")
        # Must NOT contain endpoints or full key
        assert "IngestionEndpoint" not in d["connection_string_masked"]
        assert "LiveEndpoint" not in d["connection_string_masked"]

    def test_to_dict_empty_connection_string(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        d = store.to_dict()
        assert d["connection_string_set"] is False
        assert d["connection_string_masked"] == ""
        assert d["portal_url"] == ""

    def test_to_dict_portal_url_when_provisioned(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.set_provisioned_metadata(
            app_insights_name="test-ai",
            workspace_name="test-ws",
            resource_group="test-rg",
            location="westus",
            connection_string=_FAKE_CS,
            subscription_id="sub-123",
        )
        d = store.to_dict()
        assert "portal.azure.com" in d["portal_url"]
        assert "sub-123" in d["portal_url"]
        assert "test-rg" in d["portal_url"]
        assert "test-ai" in d["portal_url"]

    def test_to_dict_grafana_dashboard_url(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.set_provisioned_metadata(
            app_insights_name="test-ai",
            workspace_name="test-ws",
            resource_group="test-rg",
            location="westus",
            connection_string=_FAKE_CS,
            subscription_id="sub-123",
        )
        d = store.to_dict()
        url = d["grafana_dashboard_url"]
        assert "portal.azure.com" in url
        assert "AzureGrafana.ReactView" in url
        assert "AgentFramework" in url
        assert "sub-123" in url
        assert "test-rg" in url
        assert "test-ai" in url
        # Slashes in the resource ID must be percent-encoded
        assert "%2F" in url

    def test_to_dict_no_grafana_url_without_subscription(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.set_provisioned_metadata(
            app_insights_name="test-ai",
            workspace_name="test-ws",
            resource_group="test-rg",
            location="westus",
            connection_string=_FAKE_CS,
        )
        d = store.to_dict()
        assert d["grafana_dashboard_url"] == ""

    def test_to_dict_full_includes_secret(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.update(connection_string=_FAKE_CS)
        d = store.to_dict_full()
        assert d["connection_string"] == _FAKE_CS

    def test_corrupted_json_falls_back_to_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "mon.json"
        path.write_text("NOT VALID JSON {{{")
        store = MonitoringConfigStore(path=path)
        assert store.enabled is False  # defaults loaded
        assert store.connection_string == ""

    def test_update_ignores_unknown_fields(self, tmp_path: Path) -> None:
        store = MonitoringConfigStore(path=tmp_path / "mon.json")
        store.update(nonexistent_field="value", enabled=True)
        assert store.enabled is True


# -----------------------------------------------------------------------
# OTel helpers (app.runtime.services.otel)
# -----------------------------------------------------------------------


class TestOtel:
    """Tests for the OTel bootstrap and span helpers."""

    @pytest.fixture(autouse=True)
    def _reset_otel(self) -> None:
        _reset_otel_state()
        yield
        _reset_otel_state()

    def test_is_active_initially_false(self) -> None:
        assert is_active() is False

    def test_get_status_when_inactive(self) -> None:
        status = get_status()
        assert status["active"] is False
        assert "tracer_provider" not in status

    @patch("app.runtime.services.otel.configure_azure_monitor", create=True)
    def test_configure_otel_success(self, mock_cam: MagicMock) -> None:
        # Patch the import inside configure_otel
        with patch.dict(
            "sys.modules",
            {"azure.monitor.opentelemetry": MagicMock(configure_azure_monitor=mock_cam)},
        ):
            result = configure_otel(_FAKE_CS, sampling_ratio=0.5)

        assert result is True
        assert is_active() is True
        mock_cam.assert_called_once_with(
            connection_string=_FAKE_CS,
            sampling_ratio=0.5,
            enable_live_metrics=False,
        )

    def test_configure_otel_empty_string(self) -> None:
        result = configure_otel("")
        assert result is False
        assert is_active() is False

    def test_configure_otel_import_error(self) -> None:
        with patch.dict("sys.modules", {"azure.monitor.opentelemetry": None}):
            result = configure_otel(_FAKE_CS)

        assert result is False
        assert is_active() is False

    def test_configure_otel_skips_when_already_active(self) -> None:
        mock_cam = MagicMock()
        with patch.dict(
            "sys.modules",
            {"azure.monitor.opentelemetry": MagicMock(configure_azure_monitor=mock_cam)},
        ):
            configure_otel(_FAKE_CS)
            result = configure_otel(_FAKE_CS)

        assert result is True
        mock_cam.assert_called_once()  # only first call

    def test_shutdown_otel_when_inactive(self) -> None:
        # Should be a no-op
        shutdown_otel()
        assert is_active() is False

    def test_shutdown_otel_when_active(self) -> None:
        mock_tp = MagicMock()
        mock_mp = MagicMock()
        mock_lp = MagicMock()

        mock_cam = MagicMock()
        with patch.dict(
            "sys.modules",
            {"azure.monitor.opentelemetry": MagicMock(configure_azure_monitor=mock_cam)},
        ):
            configure_otel(_FAKE_CS)

        with (
            patch("opentelemetry.trace.get_tracer_provider", return_value=mock_tp),
            patch("opentelemetry.metrics.get_meter_provider", return_value=mock_mp),
            patch("opentelemetry._logs.get_logger_provider", return_value=mock_lp),
        ):
            shutdown_otel()

        assert is_active() is False
        mock_tp.shutdown.assert_called_once()
        mock_mp.shutdown.assert_called_once()
        mock_lp.shutdown.assert_called_once()

    def test_agent_span_noop_when_inactive(self) -> None:
        with agent_span("test.span") as span:
            assert span is None

    def test_agent_span_creates_span_when_active(self) -> None:
        mock_cam = MagicMock()
        mock_inner_span = MagicMock()
        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_inner_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(
            "sys.modules",
            {"azure.monitor.opentelemetry": MagicMock(configure_azure_monitor=mock_cam)},
        ):
            configure_otel(_FAKE_CS)

        with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
            with agent_span("test.span", attributes={"key": "val"}) as span:
                assert span is mock_inner_span

        mock_tracer.start_as_current_span.assert_called_once_with(
            "test.span", attributes={"key": "val"}
        )

    def test_record_event_noop_when_inactive(self) -> None:
        # Should not raise
        record_event("some_event", {"detail": "value"})

    def test_record_event_when_active(self) -> None:
        mock_cam = MagicMock()
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        with patch.dict(
            "sys.modules",
            {"azure.monitor.opentelemetry": MagicMock(configure_azure_monitor=mock_cam)},
        ):
            configure_otel(_FAKE_CS)

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            record_event("agent_error", {"error": "boom"})

        mock_span.add_event.assert_called_once_with("agent_error", attributes={"error": "boom"})

    def test_set_span_attribute_noop_when_inactive(self) -> None:
        set_span_attribute("key", "value")

    def test_set_span_attribute_when_active(self) -> None:
        mock_cam = MagicMock()
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        with patch.dict(
            "sys.modules",
            {"azure.monitor.opentelemetry": MagicMock(configure_azure_monitor=mock_cam)},
        ):
            configure_otel(_FAKE_CS)

        with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
            set_span_attribute("chat.response_length", 42)

        mock_span.set_attribute.assert_called_once_with("chat.response_length", 42)

    def test_invoke_agent_span_noop_when_inactive(self) -> None:
        with invoke_agent_span("polyclaw") as span:
            assert span is None

    def test_invoke_agent_span_creates_client_span(self) -> None:
        mock_cam = MagicMock()
        mock_inner_span = MagicMock()
        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_inner_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )

        with patch.dict(
            "sys.modules",
            {"azure.monitor.opentelemetry": MagicMock(configure_azure_monitor=mock_cam)},
        ):
            configure_otel(_FAKE_CS)

        with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
            with invoke_agent_span("polyclaw", model="gpt-4.1") as span:
                assert span is mock_inner_span

        # Verify it was called with SpanKind.CLIENT and the right attributes
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "invoke_agent"
        assert call_args[1]["attributes"]["gen_ai.agent.name"] == "polyclaw"
        assert call_args[1]["attributes"]["gen_ai.request.model"] == "gpt-4.1"


# -----------------------------------------------------------------------
# MonitoringRoutes
# -----------------------------------------------------------------------

def _build_app(setup_fn) -> web.Application:
    app = web.Application()
    setup_fn(app.router)
    return app


class TestMonitoringRoutes:
    """Tests for the /api/monitoring route handler."""

    @pytest.fixture()
    def store(self, tmp_path: Path) -> MonitoringConfigStore:
        return MonitoringConfigStore(path=tmp_path / "mon.json")

    @pytest.fixture()
    def mock_az(self) -> MagicMock:
        az = MagicMock()
        az.last_stderr = ""
        az.account_info.return_value = {"id": "test-sub-id", "name": "Test Sub"}
        return az

    @pytest.fixture()
    def routes(self, store: MonitoringConfigStore) -> object:
        from app.runtime.server.routes.monitoring_routes import MonitoringRoutes

        return MonitoringRoutes(store, az=None, deploy_store=None)

    @pytest.fixture()
    def routes_with_az(
        self, store: MonitoringConfigStore, mock_az: MagicMock
    ) -> object:
        from app.runtime.server.routes.monitoring_routes import MonitoringRoutes

        return MonitoringRoutes(store, az=mock_az, deploy_store=None)

    @pytest.fixture(autouse=True)
    def _reset_otel(self) -> None:
        _reset_otel_state()
        yield
        _reset_otel_state()

    # -- GET /api/monitoring/config ----------------------------------------

    async def test_get_config_defaults(self, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/monitoring/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["enabled"] is False
            assert data["connection_string_set"] is False
            assert "otel_status" in data
            assert data["otel_status"]["active"] is False

    async def test_get_config_with_connection_string(self, store, routes) -> None:
        store.update(enabled=True, connection_string=_FAKE_CS)
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/monitoring/config")
            data = await resp.json()
            assert data["connection_string_set"] is True
            # Raw secret must not appear
            assert _FAKE_CS not in json.dumps(data)

    async def test_get_config_active_when_configured(self, store, routes) -> None:
        """otel_status.active should be True when monitoring is configured,
        even if configure_otel() was never called in this process (split mode)."""
        store.update(enabled=True, connection_string=_FAKE_CS)
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/monitoring/config")
            data = await resp.json()
            assert data["otel_status"]["active"] is True

    # -- POST /api/monitoring/config ---------------------------------------

    async def test_save_config_enable(self, store, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/monitoring/config",
                json={"enabled": True, "connection_string": _FAKE_CS},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] in ("ok", "warning")

        assert store.enabled is True
        assert store.connection_string == _FAKE_CS

    async def test_save_config_clamps_sampling(self, store, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/monitoring/config",
                json={"sampling_ratio": 2.5},
            )
        assert store.config.sampling_ratio == 1.0  # clamped to max 1.0

    async def test_save_config_disable_shuts_down_otel(self, store, routes) -> None:
        # First, mark otel as active (simulate a running session)
        mock_cam = MagicMock()
        with patch.dict(
            "sys.modules",
            {"azure.monitor.opentelemetry": MagicMock(configure_azure_monitor=mock_cam)},
        ):
            configure_otel(_FAKE_CS)

        assert is_active() is True

        store.update(enabled=True, connection_string=_FAKE_CS)
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            with (
                patch("opentelemetry.trace.get_tracer_provider", return_value=MagicMock()),
                patch("opentelemetry.metrics.get_meter_provider", return_value=MagicMock()),
                patch("opentelemetry._logs.get_logger_provider", return_value=MagicMock()),
            ):
                resp = await client.post(
                    "/api/monitoring/config",
                    json={"enabled": False},
                )
                assert resp.status == 200
                data = await resp.json()
                assert "shut down" in data["message"].lower() or "disabled" in data["message"].lower()

    # -- GET /api/monitoring/status ----------------------------------------

    async def test_get_status(self, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/monitoring/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["active"] is False

    # -- POST /api/monitoring/test -----------------------------------------

    async def test_test_connection_valid(self, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/monitoring/test",
                json={"connection_string": _FAKE_CS},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["ingestion_endpoint"]

    async def test_test_connection_empty(self, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/monitoring/test",
                json={"connection_string": ""},
            )
            assert resp.status == 400

    async def test_test_connection_missing_ikey(self, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/monitoring/test",
                json={"connection_string": "IngestionEndpoint=https://example.com/"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "instrumentationkey" in data["message"].lower()

    async def test_test_connection_missing_ingestion(self, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/monitoring/test",
                json={
                    "connection_string": "InstrumentationKey=00000000-0000-0000-0000-ffffffffffff"
                },
            )
            assert resp.status == 400
            data = await resp.json()
            assert "ingestionendpoint" in data["message"].lower()

    # -- POST /api/monitoring/provision ------------------------------------

    async def test_provision_no_az_returns_500(self, routes) -> None:
        """When AzureCLI is not available, provisioning must fail."""
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/monitoring/provision", json={})
            assert resp.status == 500
            data = await resp.json()
            assert data["status"] == "error"

    async def test_provision_already_provisioned(
        self, store, routes_with_az
    ) -> None:
        store.set_provisioned_metadata(
            app_insights_name="existing-ai",
            workspace_name="existing-ws",
            resource_group="test-rg",
            location="eastus",
            connection_string=_FAKE_CS,
        )
        app = _build_app(routes_with_az.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/monitoring/provision", json={})
            assert resp.status == 200
            data = await resp.json()
            assert "already provisioned" in data["message"].lower()

    async def test_provision_success(self, store, routes_with_az, mock_az) -> None:
        """Full provisioning flow with mocked az CLI calls."""
        mock_az.ok.return_value = Result(success=True, message="")
        mock_az.json.side_effect = [
            # 1. group show -> None (doesn't exist)
            None,
            # 2. group create
            {"id": "/subscriptions/sub/resourceGroups/rg"},
            # 3. workspace create
            {"id": "/subscriptions/sub/resourceGroups/rg/providers/...workspace_id"},
            # 4. app-insights create
            {"connectionString": _FAKE_CS, "name": "polyclaw-insights-test"},
        ]

        app = _build_app(routes_with_az.register)
        async with TestClient(TestServer(app)) as client:
            with patch(
                "app.runtime.server.routes.monitoring_routes.run_sync",
                side_effect=lambda fn, *a, **kw: fn(*a, **kw),
            ):
                mock_cam = MagicMock()
                with patch.dict(
                    "sys.modules",
                    {
                        "azure.monitor.opentelemetry": MagicMock(
                            configure_azure_monitor=mock_cam
                        )
                    },
                ):
                    resp = await client.post(
                        "/api/monitoring/provision",
                        json={"location": "eastus"},
                    )

            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert "provisioned" in data["message"].lower()

        assert store.is_provisioned is True
        assert store.enabled is True

    async def test_provision_extension_failure(
        self, store, routes_with_az, mock_az
    ) -> None:
        mock_az.ok.return_value = Result(success=False, message="extension install failed")

        app = _build_app(routes_with_az.register)
        async with TestClient(TestServer(app)) as client:
            with patch(
                "app.runtime.server.routes.monitoring_routes.run_sync",
                side_effect=lambda fn, *a, **kw: fn(*a, **kw),
            ):
                resp = await client.post(
                    "/api/monitoring/provision",
                    json={"location": "eastus"},
                )
            assert resp.status == 500
            data = await resp.json()
            assert data["status"] == "error"

    # -- DELETE /api/monitoring/provision -----------------------------------

    async def test_decommission_no_az_returns_500(self, routes) -> None:
        app = _build_app(routes.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete("/api/monitoring/provision")
            assert resp.status == 500

    async def test_decommission_not_provisioned_returns_400(
        self, routes_with_az
    ) -> None:
        app = _build_app(routes_with_az.register)
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete("/api/monitoring/provision")
            assert resp.status == 400
            data = await resp.json()
            assert "no monitoring resources provisioned" in data["message"].lower()

    async def test_decommission_success(
        self, store, routes_with_az, mock_az
    ) -> None:
        store.set_provisioned_metadata(
            app_insights_name="test-ai",
            workspace_name="test-ws",
            resource_group="polyclaw-monitoring-rg",
            location="eastus",
            connection_string=_FAKE_CS,
        )
        mock_az.ok.return_value = Result(success=True, message="")

        app = _build_app(routes_with_az.register)
        async with TestClient(TestServer(app)) as client:
            with patch(
                "app.runtime.server.routes.monitoring_routes.run_sync",
                side_effect=lambda fn, *a, **kw: fn(*a, **kw),
            ):
                resp = await client.delete("/api/monitoring/provision")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert "decommissioned" in data["message"].lower()

        assert store.is_provisioned is False
        assert store.enabled is False
        assert store.connection_string == ""
