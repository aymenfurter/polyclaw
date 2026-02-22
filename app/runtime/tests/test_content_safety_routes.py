"""Tests for Content Safety deployment routes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.runtime.server.routes.content_safety_routes import ContentSafetyRoutes
from app.runtime.services.prompt_shield import ShieldResult
from app.runtime.state.guardrails_config import GuardrailsConfigStore


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
            assert "Azure CLI" in data["message"]

    @pytest.mark.asyncio
    async def test_deploy_no_store_returns_500(self) -> None:
        az = MagicMock()
        routes = ContentSafetyRoutes(az=az, guardrails_store=None)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy",
                json={"resource_name": "test-cs"},
            )
            assert resp.status == 500
            data = await resp.json()
            assert "store" in data["message"].lower()

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.cfg")
    async def test_deploy_success(self, mock_cfg, tmp_path) -> None:
        """Full deploy flow: create, endpoint, RBAC, config updated."""
        mock_cfg.runtime_sp_app_id = "sp-app-id-1234"
        mock_cfg.aca_mi_client_id = ""

        az = MagicMock()
        az.last_stderr = ""
        # az.json calls: create, show, ad sp show (identity resolution)
        az.json.side_effect = [
            {"id": "/sub/rg/res/test-cs", "name": "test-cs"},
            {"properties": {"endpoint": "https://test-cs.cognitiveservices.azure.com/"}},
            {"id": "sp-object-id-5678", "objectId": "sp-object-id-5678"},
        ]
        az.ok.return_value = (True, "")

        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy",
                json={
                    "resource_name": "test-cs",
                    "resource_group": "test-rg",
                    "location": "westus2",
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["endpoint"] == "https://test-cs.cognitiveservices.azure.com/"
            assert data["filter_mode"] == "prompt_shields"

            # Verify all steps
            steps = {s["step"]: s["status"] for s in data["steps"]}
            assert steps["create_resource"] == "ok"
            assert steps["get_endpoint"] == "ok"
            assert steps["rbac_assign"] == "ok"
            assert steps["update_config"] == "ok"
            # No key-related steps
            assert "get_key" not in steps
            assert "store_key_kv" not in steps

            # Verify RBAC was assigned with correct args
            ok_call = az.ok.call_args
            assert "--role" in ok_call[0]
            assert "--scope" in ok_call[0]
            scope_idx = list(ok_call[0]).index("--scope")
            assert ok_call[0][scope_idx + 1] == "/sub/rg/res/test-cs"
            # Verify principal type is passed
            type_idx = list(ok_call[0]).index("--assignee-principal-type")
            assert ok_call[0][type_idx + 1] == "ServicePrincipal"

            # Verify guardrails config was updated
            assert store.config.content_safety_endpoint == (
                "https://test-cs.cognitiveservices.azure.com/"
            )
            assert store.config.filter_mode == "prompt_shields"

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.cfg")
    async def test_deploy_rbac_skip_no_identity(self, mock_cfg, tmp_path) -> None:
        """When no identity can be resolved, RBAC step warns."""
        mock_cfg.runtime_sp_app_id = ""
        mock_cfg.aca_mi_client_id = ""

        az = MagicMock()
        az.last_stderr = ""
        az.json.side_effect = [
            {"id": "/sub/rg/res/test-cs", "name": "test-cs"},
            {"properties": {"endpoint": "https://test-cs.cognitiveservices.azure.com/"}},
            None,  # signed-in-user show
        ]
        az.account_info.return_value = None

        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy", json={},
            )
            assert resp.status == 200
            data = await resp.json()
            steps = {s["step"]: s for s in data["steps"]}
            assert steps["rbac_assign"]["status"] == "warning"
            assert "manually" in steps["rbac_assign"]["detail"]

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.cfg")
    async def test_deploy_rbac_with_managed_identity(self, mock_cfg, tmp_path) -> None:
        """When ACA_MI_CLIENT_ID is set, RBAC is assigned to the MI."""
        mock_cfg.runtime_sp_app_id = ""
        mock_cfg.aca_mi_client_id = "mi-client-id-abc"

        az = MagicMock()
        az.last_stderr = ""
        az.json.side_effect = [
            {"id": "/sub/rg/res/test-cs", "name": "test-cs"},
            {"properties": {"endpoint": "https://test-cs.cognitiveservices.azure.com/"}},
            {"id": "mi-object-id-xyz"},  # ad sp show for MI
        ]
        az.ok.return_value = (True, "")

        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/deploy", json={})
            assert resp.status == 200
            data = await resp.json()
            steps = {s["step"]: s for s in data["steps"]}
            assert steps["rbac_assign"]["status"] == "ok"

            ok_call = az.ok.call_args
            oid_idx = list(ok_call[0]).index("--assignee-object-id")
            assert ok_call[0][oid_idx + 1] == "mi-object-id-xyz"

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.cfg")
    async def test_deploy_rbac_with_cli_user(self, mock_cfg, tmp_path) -> None:
        """When no SP/MI, RBAC falls back to signed-in CLI user."""
        mock_cfg.runtime_sp_app_id = ""
        mock_cfg.aca_mi_client_id = ""

        az = MagicMock()
        az.last_stderr = ""
        az.json.side_effect = [
            {"id": "/sub/rg/res/test-cs", "name": "test-cs"},
            {"properties": {"endpoint": "https://test-cs.cognitiveservices.azure.com/"}},
            {"id": "user-oid-1234"},  # signed-in-user show
        ]
        az.ok.return_value = (True, "")

        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/deploy", json={})
            assert resp.status == 200
            data = await resp.json()
            steps = {s["step"]: s for s in data["steps"]}
            assert steps["rbac_assign"]["status"] == "ok"

            ok_call = az.ok.call_args
            oid_idx = list(ok_call[0]).index("--assignee-object-id")
            assert ok_call[0][oid_idx + 1] == "user-oid-1234"
            type_idx = list(ok_call[0]).index("--assignee-principal-type")
            assert ok_call[0][type_idx + 1] == "User"

    @pytest.mark.asyncio
    async def test_deploy_create_fails(self, tmp_path) -> None:
        """When resource creation fails, route returns 500 with steps."""
        az = MagicMock()
        az.last_stderr = "Subscription not found"
        az.json.return_value = None

        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy",
                json={"resource_name": "test-cs"},
            )
            assert resp.status == 500
            data = await resp.json()
            assert data["status"] == "error"
            steps = {s["step"]: s["status"] for s in data["steps"]}
            assert steps["create_resource"] == "failed"

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.cfg")
    async def test_deploy_resource_already_exists(
        self, mock_cfg, tmp_path,
    ) -> None:
        """When resource already exists, the route reuses it."""
        mock_cfg.runtime_sp_app_id = ""
        mock_cfg.aca_mi_client_id = ""

        az = MagicMock()
        az.last_stderr = "Conflict: resource already exists"
        az.json.side_effect = [
            None,  # create returns None (conflict)
            {"id": "/sub/rg/existing-cs", "properties": {
                "endpoint": "https://existing.cognitiveservices.azure.com/",
            }},
            None,  # signed-in-user show
        ]
        az.account_info.return_value = None

        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/content-safety/deploy",
                json={"resource_name": "existing-cs"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert store.config.content_safety_endpoint == (
                "https://existing.cognitiveservices.azure.com/"
            )

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.content_safety_routes.cfg")
    async def test_deploy_uses_defaults(self, mock_cfg, tmp_path) -> None:
        """When no parameters provided, defaults are used."""
        mock_cfg.runtime_sp_app_id = ""
        mock_cfg.aca_mi_client_id = ""

        az = MagicMock()
        az.last_stderr = ""
        az.json.side_effect = [
            {"id": "/sub/rg/res"},
            {"properties": {"endpoint": "https://polyclaw-content-safety.cognitiveservices.azure.com/"}},
            None,  # signed-in-user show
        ]
        az.account_info.return_value = None

        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = ContentSafetyRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/content-safety/deploy", json={})
            assert resp.status == 200

            # Check az was called with default values
            create_call = az.json.call_args_list[0]
            args = create_call[0]
            assert "--kind" in args
            idx = list(args).index("--kind")
            assert args[idx + 1] == "ContentSafety"
            assert "--name" in args
            name_idx = list(args).index("--name")
            assert args[name_idx + 1] == "polyclaw-content-safety"


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
