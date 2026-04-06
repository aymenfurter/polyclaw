"""Root conftest — provides the ``--run-slow`` convenience flag."""

from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="Include tests marked @pytest.mark.slow (skipped by default).",
    )
    parser.addoption(
        "--run-e2e-setup",
        action="store_true",
        default=False,
        help="Include tests marked @pytest.mark.e2e_setup (skipped by default).",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-slow"):
        skip_slow = pytest.mark.skip(reason="slow test — pass --run-slow to include")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip_slow)

    if not config.getoption("--run-e2e-setup"):
        skip_e2e = pytest.mark.skip(
            reason="E2E setup test — pass --run-e2e-setup to include",
        )
        for item in items:
            if "e2e_setup" in item.keywords:
                item.add_marker(skip_e2e)
