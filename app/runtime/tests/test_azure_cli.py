"""Tests for AzureCLI and related helpers."""

from __future__ import annotations

import json
import io
import subprocess
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from app.runtime.services.cloud.azure import AzureCLI
from app.runtime.util.result import Result


class TestAzureCLIJson:
    @patch.object(AzureCLI, "_run")
    def test_success(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=0, stdout='{"name": "test"}', stderr=""
        )
        az = AzureCLI()
        result = az.json("account", "show")
        assert result == {"name": "test"}

    @patch.object(AzureCLI, "_run")
    def test_failure(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=1, stdout="", stderr="error"
        )
        az = AzureCLI()
        assert az.json("fail") is None
        assert "error" in az.last_stderr

    @patch.object(AzureCLI, "_run")
    def test_invalid_json(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=0, stdout="not json", stderr=""
        )
        az = AzureCLI()
        assert az.json("bad") is None

    @patch.object(AzureCLI, "_run")
    def test_returns_list(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=0, stdout='[{"id": 1}]', stderr=""
        )
        az = AzureCLI()
        result = az.json("resource", "list")
        assert isinstance(result, list)


class TestAzureCLIJsonCached:
    @patch.object(AzureCLI, "_run")
    def test_caches(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=0, stdout='{"cached": true}', stderr=""
        )
        az = AzureCLI()
        r1 = az.json_cached("account", "show")
        r2 = az.json_cached("account", "show")
        assert r1 == r2
        assert mock_run.call_count == 1

    @patch.object(AzureCLI, "_run")
    def test_invalidate_cache(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=0, stdout='{"cached": true}', stderr=""
        )
        az = AzureCLI()
        az.json_cached("account", "show")
        az.invalidate_cache("account", "show")
        az.json_cached("account", "show")
        assert mock_run.call_count == 2

    @patch.object(AzureCLI, "_run")
    def test_invalidate_all(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=0, stdout='{}', stderr=""
        )
        az = AzureCLI()
        az.json_cached("a")
        az.json_cached("b")
        az.invalidate_cache()
        assert len(az._cache) == 0


class TestAzureCLIOk:
    @patch.object(AzureCLI, "_run")
    def test_success(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=0, stdout="", stderr=""
        )
        az = AzureCLI()
        result = az.ok("group", "create")
        assert result.success is True

    @patch.object(AzureCLI, "_run")
    def test_failure(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            ["az"], returncode=1, stdout="", stderr="error msg"
        )
        az = AzureCLI()
        result = az.ok("group", "create")
        assert result.success is False
        assert "error msg" in result.message


class TestAzureCLIAccountInfo:
    @patch.object(AzureCLI, "json_cached")
    def test_returns_dict(self, mock_cached) -> None:
        mock_cached.return_value = {"id": "123", "name": "sub"}
        az = AzureCLI()
        assert az.account_info() == {"id": "123", "name": "sub"}

    @patch.object(AzureCLI, "json_cached")
    def test_returns_none(self, mock_cached) -> None:
        mock_cached.return_value = None
        az = AzureCLI()
        assert az.account_info() is None

    @patch.object(AzureCLI, "json_cached")
    def test_returns_none_for_list(self, mock_cached) -> None:
        mock_cached.return_value = [1, 2]
        az = AzureCLI()
        assert az.account_info() is None


class TestValidateTelegramToken:
    @patch("urllib.request.urlopen")
    def test_valid_token(self, mock_urlopen) -> None:
        resp = MagicMock()
        resp.read.return_value = json.dumps({"ok": True, "result": {"username": "testbot"}}).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        result = AzureCLI.validate_telegram_token("123:ABC", _retries=1)
        assert result.success is True
        assert "@testbot" in result.message

    @patch("urllib.request.urlopen")
    def test_invalid_token(self, mock_urlopen) -> None:
        resp = MagicMock()
        resp.read.return_value = json.dumps({"ok": False, "description": "Unauthorized"}).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        result = AzureCLI.validate_telegram_token("bad", _retries=1)
        assert result.success is False

    @patch("urllib.request.urlopen")
    def test_network_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = RuntimeError("timeout")
        result = AzureCLI.validate_telegram_token("123:ABC", _retries=1)
        assert result.success is False
        assert "Cannot reach" in result.message

    @patch("app.runtime.services.cloud.azure.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_404_not_retried(self, mock_urlopen, _mock_sleep) -> None:
        """A 404 means the bot doesn't exist -- it should NOT be retried."""
        err = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, io.BytesIO(b'{"description": "Not Found"}')
        )
        mock_urlopen.side_effect = err
        result = AzureCLI.validate_telegram_token("123:ABC", _retries=3)
        assert result.success is False
        assert "404" in result.message
        # Only one attempt -- no retries on 404.
        assert mock_urlopen.call_count == 1

    @patch("app.runtime.services.cloud.azure.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_transient_502(self, mock_urlopen, _mock_sleep) -> None:
        """A transient 502 should be retried and succeed on the next attempt."""
        err = urllib.error.HTTPError(
            "url", 502, "Bad Gateway", {}, io.BytesIO(b'{"description": "Bad Gateway"}')
        )
        ok_resp = MagicMock()
        ok_resp.read.return_value = json.dumps({"ok": True, "result": {"username": "mybot"}}).encode()
        ok_resp.__enter__ = MagicMock(return_value=ok_resp)
        ok_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [err, ok_resp]
        result = AzureCLI.validate_telegram_token("123:ABC", _retries=2)
        assert result.success is True
        assert "@mybot" in result.message
        assert mock_urlopen.call_count == 2

    def test_empty_token(self) -> None:
        result = AzureCLI.validate_telegram_token("", _retries=1)
        assert result.success is False
        assert "empty" in result.message.lower()

    def test_kv_ref_token(self) -> None:
        result = AzureCLI.validate_telegram_token("@kv:infra-token", _retries=1)
        assert result.success is False
        assert "Key Vault" in result.message


class TestAzureCLIGetChannels:
    @patch.object(AzureCLI, "json")
    def test_no_config(self, mock_json) -> None:
        az = AzureCLI()
        with patch("app.runtime.config.settings.cfg") as mock_cfg:
            mock_cfg.env = MagicMock()
            mock_cfg.env.read.return_value = ""
            result = az.get_channels()
        assert result == {}

    @patch.object(AzureCLI, "json")
    def test_with_telegram(self, mock_json) -> None:
        mock_json.return_value = {
            "properties": {"configuredChannels": ["webchat", "telegram"]}
        }
        az = AzureCLI()
        with patch("app.runtime.config.settings.cfg") as mock_cfg:
            mock_cfg.env = MagicMock()
            mock_cfg.env.read.side_effect = lambda k: "rg" if k == "BOT_RESOURCE_GROUP" else "bot"
            result = az.get_channels()
        assert result.get("telegram") is True


class TestAzureCLIUpdateEndpoint:
    @patch.object(AzureCLI, "json")
    def test_not_configured(self, mock_json) -> None:
        az = AzureCLI()
        with patch("app.runtime.config.settings.cfg") as mock_cfg:
            mock_cfg.env = MagicMock()
            mock_cfg.env.read.return_value = ""
            result = az.update_endpoint("https://example.com/api/messages")
        assert result.success is False
        assert "not configured" in result.message.lower()
