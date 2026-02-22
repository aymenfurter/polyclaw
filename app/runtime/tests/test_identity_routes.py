"""Tests for Agent Identity routes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.runtime.server.routes.identity_routes import IdentityRoutes
from app.runtime.state.guardrails_config import GuardrailsConfigStore


def _build_app(routes: IdentityRoutes) -> web.Application:
    app = web.Application()
    routes.register(app.router)
    return app


class TestIdentityInfo:
    """GET /api/identity/info."""

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_info_no_identity(self, mock_cfg) -> None:
        mock_cfg.runtime_sp_app_id = ""
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = ""
        routes = IdentityRoutes(az=None)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/identity/info")
            assert resp.status == 200
            data = await resp.json()
            assert data["configured"] is False
            assert data["strategy"] is None

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_info_with_sp(self, mock_cfg) -> None:
        mock_cfg.runtime_sp_app_id = "app-id-123"
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = "tenant-456"

        az = MagicMock()
        az.json.return_value = {
            "displayName": "polyclaw-runtime",
            "id": "obj-id-789",
        }

        routes = IdentityRoutes(az=az)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/identity/info")
            assert resp.status == 200
            data = await resp.json()
            assert data["configured"] is True
            assert data["strategy"] == "service_principal"
            assert data["display_name"] == "polyclaw-runtime"
            assert data["principal_id"] == "obj-id-789"
            assert data["tenant"] == "tenant-456"

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_info_with_mi(self, mock_cfg) -> None:
        mock_cfg.runtime_sp_app_id = ""
        mock_cfg.aca_mi_client_id = "mi-client-abc"
        mock_cfg.runtime_sp_tenant = ""

        az = MagicMock()
        az.json.return_value = {
            "appDisplayName": "polyclaw-mi",
            "id": "mi-oid",
        }

        routes = IdentityRoutes(az=az)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/identity/info")
            assert resp.status == 200
            data = await resp.json()
            assert data["configured"] is True
            assert data["strategy"] == "managed_identity"
            assert data["display_name"] == "polyclaw-mi"


class TestIdentityRoles:
    """GET /api/identity/roles."""

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_roles_no_az(self, mock_cfg) -> None:
        mock_cfg.runtime_sp_app_id = "x"
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = ""
        routes = IdentityRoutes(az=None)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/identity/roles")
            assert resp.status == 200
            data = await resp.json()
            assert data["assignments"] == []

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_roles_with_assignments(self, mock_cfg) -> None:
        mock_cfg.runtime_sp_app_id = "app-id"
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = ""

        az = MagicMock()
        az.json.side_effect = [
            {"id": "obj-id-resolved"},  # _sp_show
            [  # role assignment list
                {
                    "roleDefinitionName": "Cognitive Services User",
                    "scope": "/sub/rg/cs",
                    "condition": "",
                },
                {
                    "roleDefinitionName": "Reader",
                    "scope": "/sub/rg",
                    "condition": "",
                },
            ],
        ]

        routes = IdentityRoutes(az=az)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/identity/roles")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["assignments"]) == 2
            assert len(data["checks"]) == 5

            checks = {c["role"]: c["present"] for c in data["checks"]}
            assert checks["Cognitive Services User"] is True
            assert checks["Reader"] is True
            assert checks["Azure Bot Service Contributor Role"] is False
            assert checks["Key Vault Secrets Officer"] is False
            assert checks["Azure ContainerApps Session Executor"] is False

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_roles_sp_show_fails_uses_app_id(self, mock_cfg) -> None:
        """When az ad sp show fails, _roles falls back to app_id."""
        mock_cfg.runtime_sp_app_id = "app-id"
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = ""

        az = MagicMock()
        az.json.side_effect = [
            None,  # _sp_show fails
            [{"roleDefinitionName": "Reader", "scope": "/sub/rg", "condition": ""}],
        ]

        routes = IdentityRoutes(az=az)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/identity/roles")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["assignments"]) == 1
            # Second call should use app-id as assignee
            call_args = az.json.call_args_list[1]
            assert "app-id" in call_args[0]

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_roles_list_failure(self, mock_cfg) -> None:
        mock_cfg.runtime_sp_app_id = "app-id"
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = ""

        az = MagicMock()
        az.json.side_effect = [
            None,   # _sp_show fails
            None,   # role assignment list also fails
        ]
        az.last_stderr = "auth error"

        routes = IdentityRoutes(az=az)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/identity/roles")
            assert resp.status == 500
            data = await resp.json()
            assert data["status"] == "error"


class TestIdentityFixRoles:
    """POST /api/identity/fix-roles."""

    @pytest.mark.asyncio
    async def test_fix_no_az(self) -> None:
        routes = IdentityRoutes(az=None)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/identity/fix-roles")
            assert resp.status == 400

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_fix_skips_without_endpoint(self, mock_cfg, tmp_path) -> None:
        mock_cfg.runtime_sp_app_id = "app-id"
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = ""

        az = MagicMock()
        az.json.side_effect = [
            {"id": "sp-oid", "objectId": "sp-oid"},  # resolve principal
        ]

        store = GuardrailsConfigStore(tmp_path / "g.json")
        routes = IdentityRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/identity/fix-roles")
            assert resp.status == 200
            data = await resp.json()
            assert data["steps"][0]["status"] == "skipped"

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_fix_assigns_role(self, mock_cfg, tmp_path) -> None:
        mock_cfg.runtime_sp_app_id = "app-id"
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = ""

        az = MagicMock()
        az.last_stderr = ""
        az.json.side_effect = [
            {"id": "sp-oid"},  # resolve principal
            [{"id": "/sub/rg/cs", "properties": {
                "endpoint": "https://my-cs.cognitiveservices.azure.com/",
            }}],  # account list (scoped to RG)
        ]
        az.ok.return_value = (True, "")

        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://my-cs.cognitiveservices.azure.com/")
        routes = IdentityRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/identity/fix-roles")
            assert resp.status == 200
            data = await resp.json()
            step = data["steps"][0]
            assert step["status"] == "ok"
            assert "Cognitive Services User" in step["step"] or "cognitive" in step["step"]
            # Verify --assignee-object-id was used (object ID resolved)
            ok_call = az.ok.call_args
            assert "--assignee-object-id" in ok_call[0]

    @pytest.mark.asyncio
    @patch("app.runtime.server.routes.identity_routes.cfg")
    async def test_fix_falls_back_to_assignee_on_sp_failure(self, mock_cfg, tmp_path) -> None:
        """When az ad sp show fails, fix-roles uses --assignee with app_id."""
        mock_cfg.runtime_sp_app_id = "app-id"
        mock_cfg.aca_mi_client_id = ""
        mock_cfg.runtime_sp_tenant = ""

        az = MagicMock()
        az.last_stderr = ""
        az.json.side_effect = [
            None,  # resolve principal fails (CAE error)
            [{"id": "/sub/rg/cs", "properties": {
                "endpoint": "https://my-cs.cognitiveservices.azure.com/",
            }}],  # account list
        ]
        az.ok.return_value = (True, "")

        store = GuardrailsConfigStore(tmp_path / "g.json")
        store.set_content_safety_endpoint("https://my-cs.cognitiveservices.azure.com/")
        routes = IdentityRoutes(az=az, guardrails_store=store)
        app = _build_app(routes)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/identity/fix-roles")
            assert resp.status == 200
            data = await resp.json()
            step = data["steps"][0]
            assert step["status"] == "ok"
            # Verify --assignee was used instead of --assignee-object-id
            ok_call = az.ok.call_args
            assert "--assignee" in ok_call[0]
            assert "--assignee-object-id" not in ok_call[0]
