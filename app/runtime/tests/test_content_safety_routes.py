"""Tests for Content Safety deployment routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.runtime.server.routes.content_safety_routes import ContentSafetyRoutes
from app.runtime.services.deployment.bicep_deployer import BicepDeployResult
from app.runtime.services.security.prompt_shield import ShieldResult
from app.runtime.state.guardrails import GuardrailsConfigStore


def _build_app(routes: ContentSafetyRoutes) -> web.Application:
    app = web.Application()
    routes.register(app.router)
    return app


class TestContentSafetyStatus:
    """GET /api/content-safety/status."""

    @pytest.mark.asyncio
    async def test_status_no_store(self) -> None:
        routes = ContentSafetyRoutes(az=None, guardrails_store=None)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/content-safety/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["deployed"] is False

    @pytest.mark.asyncio
    async def test_status_not_configured(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=None, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/content-safety/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["deployed"] is False
            assert data["endpoint"] == ""

    @pytest.mark.asyncio
    async def test_status_configured(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://test.cognitiveservices.azure.com")
        store.set_filter_mode("prompt_shields")
        routes = ContentSafetyRoutes(az=None, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/content-safety/status")
            assert resp.status == 200
            data = await resp.json()
            assert data["deployed"] is True
            assert data["filter_mode"] == "prompt_shields"


class TestContentSafetyDeploy:
    """POST /api/content-safety/deploy."""

    @pytest.mark.asyncio
    async def test_deploy_no_az_returns_400(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=None, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy",
                json={"resource_name": "test-cs"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "not available" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_deploy_no_store_returns_error(self) -> None:
        az = MagicMock()
        routes = ContentSafetyRoutes(az=az, guardrails_store=None)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy",
                json={"resource_name": "test-cs"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "not available" in data["message"].lower()

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.run_sync", new_callable=AsyncMock)
    async def test_deploy_success(self, mock_run_sync, tmp_path) -> None:
        """Full deploy flow via Bicep: returns endpoint, config updated."""
        result = BicepDeployResult(
            ok=True,
            deploy_id="test-deploy-id",
            content_safety_endpoint="https://test-cs.cognitiveservices.azure.com/",
            content_safety_name="test-cs",
            steps=[
                {"step": "bicep_deploy", "status": "ok", "detail": "Deployed"},
            ],
        )
        mock_run_sync.return_value = result

        az = MagicMock()
        deploy_store = MagicMock()
        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(
            az=az, guardrails_store=store, deploy_store=deploy_store,
        )
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy",
                json={"resource_group": "test-rg", "location": "westus2"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["endpoint"] == "https://test-cs.cognitiveservices.azure.com/"
            assert data["filter_mode"] == "prompt_shields"

            steps = {s["step"]: s["status"] for s in data["steps"]}
            assert steps["bicep_deploy"] == "ok"
            assert steps["update_config"] == "ok"

            # Verify guardrails config was updated
            assert store.config.content_safety_endpoint == (
                "https://test-cs.cognitiveservices.azure.com/"
            )
            assert store.config.filter_mode == "prompt_shields"

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.run_sync", new_callable=AsyncMock)
    async def test_deploy_bicep_fails(self, mock_run_sync, tmp_path) -> None:
        """When Bicep deployment fails, route returns 500 with steps."""
        result = BicepDeployResult(
            ok=False,
            error="Subscription not found",
            steps=[
                {"step": "bicep_deploy", "status": "failed", "detail": "Subscription not found"},
            ],
        )
        mock_run_sync.return_value = result

        az = MagicMock()
        deploy_store = MagicMock()
        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(
            az=az, guardrails_store=store, deploy_store=deploy_store,
        )
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy",
                json={"resource_name": "test-cs"},
            )
            assert resp.status == 500
            data = await resp.json()
            assert data["status"] == "error"
            assert "Subscription not found" in data["message"]

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.run_sync", new_callable=AsyncMock)
    async def test_deploy_no_endpoint_returns_error(self, mock_run_sync, tmp_path) -> None:
        """When Bicep succeeds but no endpoint, returns 500."""
        result = BicepDeployResult(
            ok=True,
            deploy_id="test-deploy",
            content_safety_endpoint="",
            steps=[],
        )
        mock_run_sync.return_value = result

        az = MagicMock()
        deploy_store = MagicMock()
        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(
            az=az, guardrails_store=store, deploy_store=deploy_store,
        )
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/deploy", json={})
            assert resp.status == 500

    @pytest.mark.asyncio
    async def test_deploy_no_guardrails_store_returns_500(self, tmp_path) -> None:
        """When az and deploy_store are present but no guardrails store."""
        az = MagicMock()
        deploy_store = MagicMock()
        routes = ContentSafetyRoutes(
            az=az, guardrails_store=None, deploy_store=deploy_store,
        )
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/deploy", json={})
            assert resp.status == 500
            data = await resp.json()
            assert "store" in data["message"].lower()


class TestContentSafetyEnsureRbac:
    """ContentSafetyRoutes.ensure_rbac -- startup RBAC reconciliation."""

    @pytest.mark.asyncio
    async def test_noop_without_az(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://x.cognitiveservices.azure.com")
        routes = ContentSafetyRoutes(az=None, guardrails_store=store)
        assert await routes.ensure_rbac() == []

    @pytest.mark.asyncio
    async def test_noop_without_store(self) -> None:
        routes = ContentSafetyRoutes(az=MagicMock(), guardrails_store=None)
        assert await routes.ensure_rbac() == []

    @pytest.mark.asyncio
    async def test_noop_no_endpoint(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=MagicMock(), guardrails_store=store)
        assert await routes.ensure_rbac() == []

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.cfg")
    async def test_assigns_role(self, mock_cfg, tmp_path) -> None:
        mock_cfg.runtime_sp_app_id = "sp-id"
        mock_cfg.aca_mi_client_id = ""

        az = MagicMock()
        az.last_stderr = ""
        az.json.side_effect = [
            # cognitiveservices account list
            [{"id": "/sub/rg/cs-res", "properties": {
                "endpoint": "https://my-cs.cognitiveservices.azure.com/",
            }}],
            # ad sp show
            {"id": "sp-oid-123"},
        ]
        az.ok.return_value = (True, "")

        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://my-cs.cognitiveservices.azure.com/")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        steps = await routes.ensure_rbac()

        status_map = {s["step"]: s["status"] for s in steps}
        assert status_map["resolve_resource"] == "ok"
        assert status_map["rbac_assign"] == "ok"

        ok_call = az.ok.call_args
        scope_idx = list(ok_call[0]).index("--scope")
        assert ok_call[0][scope_idx + 1] == "/sub/rg/cs-res"

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.cfg")
    async def test_warns_no_matching_resource(self, mock_cfg, tmp_path) -> None:
        mock_cfg.runtime_sp_app_id = "sp-id"
        mock_cfg.aca_mi_client_id = ""

        az = MagicMock()
        az.last_stderr = ""
        az.json.return_value = []

        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://gone.cognitiveservices.azure.com/")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        steps = await routes.ensure_rbac()

        status_map = {s["step"]: s["status"] for s in steps}
        assert status_map["resolve_resource"] == "warning"
        assert "No account matched" in steps[0]["detail"]

    @pytest.mark.asyncio
    async def test_warns_list_failure(self, tmp_path) -> None:
        az = MagicMock()
        az.last_stderr = "auth error"
        az.json.return_value = None

        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://x.cognitiveservices.azure.com")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        steps = await routes.ensure_rbac()

        assert len(steps) == 1
        assert steps[0]["status"] == "warning"
        assert "Failed to list" in steps[0]["detail"]


class TestContentSafetyTest:
    """POST /api/content-safety/test -- dry-run probe."""

    @pytest.mark.asyncio
    async def test_test_no_store(self) -> None:
        routes = ContentSafetyRoutes(az=None, guardrails_store=None)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/test")
            assert resp.status == 500
            data = await resp.json()
            assert "store" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_test_no_endpoint(self, tmp_path) -> None:
        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=None, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/test")
            assert resp.status == 200
            data = await resp.json()
            assert data["passed"] is False
            assert "deploy" in data["detail"].lower()

    @pytest.mark.asyncio
    @patch(
        "app.runtime.server.routes.content_safety_routes.PromptShieldService",
        autospec=True,
    )
    async def test_test_success(self, MockShield, tmp_path) -> None:
        """Dry-run passes when shield.dry_run reports no attack."""
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://x.cognitiveservices.azure.com")
        routes = ContentSafetyRoutes(az=None, guardrails_store=store)
        app = _build_app(routes)

        instance = MockShield.return_value
        instance.dry_run.return_value = ShieldResult(
            attack_detected=False,
            mode="prompt_shields",
            detail="API reachable, auth OK",
        )

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/test")
            assert resp.status == 200
            data = await resp.json()
            assert data["passed"] is True
            assert "OK" in data["detail"]

    @pytest.mark.asyncio
    @patch(
        "app.runtime.server.routes.content_safety_routes.PromptShieldService",
        autospec=True,
    )
    async def test_test_auth_failure(self, MockShield, tmp_path) -> None:
        """Dry-run fails when shield.dry_run reports auth error."""
        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://x.cognitiveservices.azure.com")
        routes = ContentSafetyRoutes(az=None, guardrails_store=store)
        app = _build_app(routes)

        instance = MockShield.return_value
        instance.dry_run.return_value = ShieldResult(
            attack_detected=True,
            mode="prompt_shields",
            detail="HTTP 401: PermissionDenied",
        )

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/test")
            assert resp.status == 200
            data = await resp.json()
            assert data["passed"] is False
            assert "401" in data["detail"]
