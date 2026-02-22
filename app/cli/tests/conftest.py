"""Shared pytest fixtures for app.cli tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("POLYCLAW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("POLYCLAW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("DOTENV_PATH", str(tmp_path / ".env"))
    return data_dir


@pytest.fixture(autouse=True)
def _reset_singletons(_isolate_data_dir: Path):
    from app.runtime.util.singletons import reset_all_singletons

    reset_all_singletons()
    yield
    reset_all_singletons()


@pytest.fixture()
def data_dir(_isolate_data_dir: Path) -> Path:
    return _isolate_data_dir
