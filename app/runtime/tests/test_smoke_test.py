"""Tests for SmokeTestRunner and _StateFileValidator."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.runtime.server.smoke_test import SmokeTestRunner, _StateFileValidator


class TestStateFileValidator:
    def test_valid_dict(self, tmp_path: Path) -> None:
        f = tmp_path / "ok.json"
        f.write_text(json.dumps({"a": 1}))
        assert _StateFileValidator().check(f) is None

    def test_valid_list(self, tmp_path: Path) -> None:
        f = tmp_path / "ok.json"
        f.write_text(json.dumps([1, 2, 3]))
        assert _StateFileValidator().check(f) is None

    def test_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("not json {{{")
        err = _StateFileValidator().check(f)
        assert err is not None
        assert "invalid JSON" in err

    def test_read_error(self) -> None:
        err = _StateFileValidator().check(Path("/nonexistent/file.json"))
        assert err is not None
        assert "read error" in err

    def test_unexpected_type(self, tmp_path: Path) -> None:
        f = tmp_path / "str.json"
        f.write_text('"just a string"')
        err = _StateFileValidator().check(f)
        assert err is not None
        assert "expected dict or list" in err

    def test_missing_required_key(self, tmp_path: Path) -> None:
        f = tmp_path / "miss.json"
        f.write_text(json.dumps({"a": 1}))
        err = _StateFileValidator(required_keys=["b"]).check(f)
        assert err is not None
        assert "missing key 'b'" in err

    def test_has_required_key(self, tmp_path: Path) -> None:
        f = tmp_path / "ok.json"
        f.write_text(json.dumps({"b": 2}))
        assert _StateFileValidator(required_keys=["b"]).check(f) is None

    def test_type_check_pass(self, tmp_path: Path) -> None:
        f = tmp_path / "tc.json"
        f.write_text(json.dumps({"items": [1, 2]}))
        assert _StateFileValidator(type_checks={"items": list}).check(f) is None

    def test_type_check_fail(self, tmp_path: Path) -> None:
        f = tmp_path / "tc.json"
        f.write_text(json.dumps({"items": "not-a-list"}))
        err = _StateFileValidator(type_checks={"items": list}).check(f)
        assert err is not None
        assert "should be list" in err

    def test_type_check_missing_key_ok(self, tmp_path: Path) -> None:
        f = tmp_path / "tc.json"
        f.write_text(json.dumps({"other": 1}))
        assert _StateFileValidator(type_checks={"items": list}).check(f) is None

    def test_multiple_required_keys(self, tmp_path: Path) -> None:
        f = tmp_path / "multi.json"
        f.write_text(json.dumps({"a": 1, "b": 2}))
        assert _StateFileValidator(required_keys=["a", "b"]).check(f) is None

    def test_list_skips_required_keys(self, tmp_path: Path) -> None:
        f = tmp_path / "list.json"
        f.write_text(json.dumps([1, 2, 3]))
        assert _StateFileValidator(required_keys=["a"]).check(f) is None


class TestSmokeTestRunnerHelpers:
    def _make_runner(self) -> SmokeTestRunner:
        return SmokeTestRunner(MagicMock())

    def test_step_records(self) -> None:
        r = self._make_runner()
        r._step("test", True, "detail")
        assert len(r._steps) == 1
        assert r._steps[0] == {"step": "test", "ok": True, "detail": "detail"}

    def test_step_default_detail(self) -> None:
        r = self._make_runner()
        r._step("test", True)
        assert r._steps[0]["detail"] == ""

    def test_fail_result(self) -> None:
        r = self._make_runner()
        r._step("a", True)
        result = r._fail("oops")
        assert result["status"] == "error"
        assert result["message"] == "oops"
        assert len(result["steps"]) == 1

    @patch("shutil.which", return_value="/usr/bin/copilot")
    def test_check_cli_found(self, _mock) -> None:
        r = self._make_runner()
        assert r._check_cli() is True
        assert r._steps[-1]["ok"] is True

    @patch("shutil.which", return_value=None)
    def test_check_cli_not_found(self, _mock) -> None:
        r = self._make_runner()
        assert r._check_cli() is False
        assert r._steps[-1]["ok"] is False

    def test_check_auth_gh_authenticated(self) -> None:
        r = self._make_runner()
        r._gh.status.return_value = {"authenticated": True, "details": "ok"}
        result = r._check_auth()
        assert result is True
        assert r._steps[-1]["ok"] is True

    def test_check_auth_no_auth(self) -> None:
        r = self._make_runner()
        r._gh.status.return_value = {"authenticated": False, "details": "not logged in"}
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.foundry_endpoint = ""
            result = r._check_auth()
        assert result is False

    @patch("subprocess.run")
    def test_check_version_success(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="1.2.3", stderr="")
        r = self._make_runner()
        r._check_version()
        assert r._steps[-1]["ok"] is True
        assert "1.2.3" in r._steps[-1]["detail"]

    @patch("subprocess.run")
    def test_check_version_nonzero_rc(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        r = self._make_runner()
        r._check_version()
        assert r._steps[-1]["ok"] is False

    @patch("subprocess.run")
    def test_check_version_exception(self, mock_run) -> None:
        mock_run.side_effect = FileNotFoundError("no copilot")
        r = self._make_runner()
        r._check_version()
        assert r._steps[-1]["ok"] is False

    @patch("shutil.which", return_value="/usr/bin/node")
    def test_probe_local_mcp_found(self, _) -> None:
        r = self._make_runner()
        r._probe_local_mcp("mcp_test", {"command": "node"})
        assert r._steps[-1]["ok"] is True

    @patch("shutil.which", return_value=None)
    def test_probe_local_mcp_not_found(self, _) -> None:
        r = self._make_runner()
        r._probe_local_mcp("mcp_test", {"command": "nonexistent"})
        assert r._steps[-1]["ok"] is False

    def test_probe_local_mcp_no_command(self) -> None:
        r = self._make_runner()
        r._probe_local_mcp("mcp_test", {"command": ""})
        assert r._steps[-1]["ok"] is False

    def test_check_state_files_no_dir(self) -> None:
        r = self._make_runner()
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.data_dir = Path("/nonexistent/path/abc123")
            r._check_state_files()
        assert r._steps[-1]["ok"] is True
        assert "does not exist" in r._steps[-1]["detail"]

    def test_check_state_files_empty_dir(self, tmp_path: Path) -> None:
        r = self._make_runner()
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            r._check_state_files()
        assert r._steps[-1]["ok"] is True

    def test_check_state_files_valid(self, tmp_path: Path) -> None:
        (tmp_path / "agent_profile.json").write_text(json.dumps({"name": "Bot"}))
        r = self._make_runner()
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            r._check_state_files()
        assert r._steps[-1]["ok"] is True
        assert "1 state file" in r._steps[-1]["detail"]

    def test_check_state_files_invalid(self, tmp_path: Path) -> None:
        (tmp_path / "agent_profile.json").write_text("not json")
        r = self._make_runner()
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            r._check_state_files()
        assert r._steps[-1]["ok"] is False

    def test_check_state_files_infra(self, tmp_path: Path) -> None:
        (tmp_path / "infra.json").write_text(json.dumps({
            "bot": {"id": "b1"}, "channels": {"web": True}
        }))
        r = self._make_runner()
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            r._check_state_files()
        assert r._steps[-1]["ok"] is True

    def test_check_state_files_sessions(self, tmp_path: Path) -> None:
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "s1.json").write_text(json.dumps({"id": "s1", "messages": []}))
        r = self._make_runner()
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            r._check_state_files()
        assert r._steps[-1]["ok"] is True

    def test_check_state_files_invalid_session(self, tmp_path: Path) -> None:
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "bad.json").write_text("not json")
        r = self._make_runner()
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            r._check_state_files()
        assert r._steps[-1]["ok"] is False

    def test_check_state_files_extra_json(self, tmp_path: Path) -> None:
        (tmp_path / "custom.json").write_text(json.dumps({"custom": True}))
        r = self._make_runner()
        with patch("app.runtime.server.smoke_test.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            r._check_state_files()
        assert r._steps[-1]["ok"] is True

    @patch("app.runtime.services.keyvault.kv")
    def test_check_keyvault_disabled(self, mock_kv) -> None:
        mock_kv.enabled = False
        r = self._make_runner()
        r._check_keyvault()
        assert r._steps[-1]["ok"] is True
        assert "Not configured" in r._steps[-1]["detail"]

    @patch("app.runtime.services.keyvault.kv")
    def test_check_keyvault_connected(self, mock_kv) -> None:
        mock_kv.enabled = True
        mock_kv.url = "https://test.vault.azure.net"
        mock_kv.list_secrets.return_value = ["s1", "s2"]
        r = self._make_runner()
        r._check_keyvault()
        assert r._steps[-1]["ok"] is True
        assert "2 secret(s)" in r._steps[-1]["detail"]

    @patch("app.runtime.services.keyvault.kv")
    def test_check_keyvault_error(self, mock_kv) -> None:
        mock_kv.enabled = True
        mock_kv.url = "https://test.vault.azure.net"
        mock_kv.list_secrets.side_effect = RuntimeError("denied")
        r = self._make_runner()
        r._check_keyvault()
        assert r._steps[-1]["ok"] is False

    @pytest.mark.asyncio
    async def test_check_mcp_no_servers(self) -> None:
        with patch("app.runtime.state.mcp_config.McpConfigStore") as MockStore:
            MockStore.return_value.list_servers.return_value = []
            r = self._make_runner()
            await r._check_mcp_servers()
        assert r._steps[-1]["ok"] is True
        assert "No MCP" in r._steps[-1]["detail"]

    @pytest.mark.asyncio
    async def test_check_mcp_local_server(self) -> None:
        server = {"name": "test", "enabled": True, "type": "stdio", "command": "node"}
        with patch("app.runtime.state.mcp_config.McpConfigStore") as MockStore, \
             patch("shutil.which", return_value="/usr/bin/node"):
            MockStore.return_value.list_servers.return_value = [server]
            r = self._make_runner()
            await r._check_mcp_servers()
        assert r._steps[-1]["ok"] is True

    @pytest.mark.asyncio
    async def test_check_mcp_unknown_type(self) -> None:
        server = {"name": "test", "enabled": True, "type": "grpc"}
        with patch("app.runtime.state.mcp_config.McpConfigStore") as MockStore:
            MockStore.return_value.list_servers.return_value = [server]
            r = self._make_runner()
            await r._check_mcp_servers()
        assert r._steps[-1]["ok"] is False
        assert "Unknown type" in r._steps[-1]["detail"]