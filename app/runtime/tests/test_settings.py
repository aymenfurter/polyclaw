"""Tests for the Settings module."""

from __future__ import annotations

from pathlib import Path

from app.runtime.config.settings import Settings


class TestSettings:
    def test_defaults(self, data_dir: Path) -> None:
        s = Settings()
        assert s.copilot_model == "claude-sonnet-4.6"
        assert s.admin_port == 9090
        assert s.bot_port == 3978

    def test_write_env_and_reload(self, data_dir: Path) -> None:
        s = Settings()
        s.write_env(COPILOT_MODEL="test-model")
        assert s.copilot_model == "test-model"

    def test_ensure_dirs(self, data_dir: Path) -> None:
        s = Settings()
        s.ensure_dirs()
        assert s.media_dir.is_dir()
        assert s.memory_dir.is_dir()
        assert s.sessions_dir.is_dir()

    def test_lockdown_mode_defaults_false(self, data_dir: Path) -> None:
        s = Settings()
        assert s.lockdown_mode is False

    def test_telegram_whitelist_empty(self, data_dir: Path) -> None:
        s = Settings()
        assert s.telegram_whitelist == frozenset()

    def test_telegram_whitelist_parsed(self, data_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("TELEGRAM_WHITELIST", "123,456,789")
        s = Settings()
        assert s.telegram_whitelist == frozenset({"123", "456", "789"})

    def test_acs_callback_token_generated(self, data_dir: Path) -> None:
        s = Settings()
        assert len(s.acs_callback_token) > 0

    def test_acs_resource_id_derived(self, data_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("ACS_CONNECTION_STRING", "endpoint=https://myacs.communication.azure.com/;accesskey=abc")
        s = Settings()
        # Resource ID is empty until auto-learned from first ACS JWT
        assert s.acs_resource_id == ""

    def test_acs_resource_id_empty_without_conn(self, data_dir: Path) -> None:
        s = Settings()
        assert s.acs_resource_id == ""

    def test_data_dir_from_env(self, data_dir: Path) -> None:
        s = Settings()
        assert s.data_dir == data_dir
