"""Shared pytest fixtures for app.runtime tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request) -> Path:
    # E2E tests drive Docker containers externally -- skip isolation.
    if any(m.name == "e2e_setup" for m in request.node.iter_markers()):
        yield tmp_path
        return
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("POLYCLAW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("POLYCLAW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("DOTENV_PATH", str(tmp_path / ".env"))
    yield data_dir


@pytest.fixture(autouse=True)
def _reset_singletons(_isolate_data_dir: Path, request):
    # E2E tests drive Docker containers externally -- skip singleton reset.
    if any(m.name == "e2e_setup" for m in request.node.iter_markers()):
        yield
        return
    from app.runtime.util.singletons import reset_all_singletons

    reset_all_singletons()
    yield
    reset_all_singletons()


@pytest.fixture()
def data_dir(_isolate_data_dir: Path) -> Path:
    return _isolate_data_dir


@pytest.fixture()
def mock_agent() -> AsyncMock:
    agent = AsyncMock()
    agent.has_session = True
    agent.request_counts = {}
    agent.send.return_value = "mock response"
    return agent
