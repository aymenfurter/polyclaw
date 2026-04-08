"""End-to-end lifecycle test for ACA deployment via the TUI headless mode.

Uses the TUI's ``aca-setup``, ``aca-restart``, ``run``, ``health``, and
``aca-decommission`` CLI modes so the test exercises the same code path a
real user deploying to Azure Container Apps would follow.

Architecture: local admin (permanent) + runtime on ACA.

  1. ``bun run src/index.ts aca-setup`` -- build images, start admin,
     Azure check, Foundry deploy, ACA deploy, chat probe.
  2. ``bun run src/index.ts aca-restart`` + ``run`` -- verify chat
     survives an ACA revision restart.
  3. ``docker restart polyclaw-admin`` + ``run`` -- verify chat survives
     admin container restart (ACA runtime stays up).
  4. ``bun run src/index.ts aca-decommission`` -- tear down ACA + Foundry.

Usage::

    pytest app/runtime/tests/test_e2e_aca_lifecycle.py --run-e2e-setup -s -v

Requirements:
    - Docker + Bun running locally
    - Active ``az login`` session (TUI bind-mounts ``~/.azure``)
    - Sufficient Azure quota in the target region
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import time
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TUI_DIR = _PROJECT_ROOT / "app" / "tui"
_ADMIN_CONTAINER = "polyclaw-admin"
_ADMIN_URL = "http://localhost:9090"
_HEALTH_URL = f"{_ADMIN_URL}/health"

_BOOT_TIMEOUT = 120
_HEALTH_POLL = 5

_RG = "polyclaw-e2e-aca-rg"
_LOCATION = "eastus"
_BASE_NAME = "ac" + os.urandom(3).hex()
_SUBSCRIPTION = os.environ.get(
    "E2E_SUBSCRIPTION_ID", "546bf80c-9de8-4f7c-95db-43b72afbec60",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=timeout, check=check, cwd=cwd or _PROJECT_ROOT,
    )


def _compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", *args], timeout=timeout)


def _tui(
    *args: str,
    timeout: int = 60,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a TUI CLI command via ``bun run src/index.ts <args>``."""
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        ["bun", "run", "src/index.ts", *args],
        capture_output=True, text=True,
        timeout=timeout, check=check, cwd=_TUI_DIR, env=env,
    )


def _aca_setup_env() -> dict[str, str]:
    """Environment variables for the TUI ``aca-setup`` mode."""
    return {
        "POLYCLAW_SETUP_RG": _RG,
        "POLYCLAW_SETUP_LOCATION": _LOCATION,
        "POLYCLAW_SETUP_BASE_NAME": _BASE_NAME,
        "POLYCLAW_SETUP_SUBSCRIPTION_ID": _SUBSCRIPTION,
    }


def _tui_run_chat(timeout: int = 180) -> tuple[str, int]:
    """Send a chat probe via ``bun run src/index.ts run``."""
    r = _tui(
        "run", "Reply with exactly: PROBE_OK",
        timeout=timeout, check=False,
    )
    return (r.stdout + r.stderr).strip(), r.returncode


def _poll_health(timeout: float = _BOOT_TIMEOUT) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = _run(["curl", "-sf", "--max-time", "5", _HEALTH_URL], check=False, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        time.sleep(_HEALTH_POLL)
    return None


def _container_logs(container: str, tail: int = 200) -> str:
    try:
        r = _run(["docker", "logs", "--tail", str(tail), container], check=False, timeout=15)
        return (r.stdout + r.stderr).strip()
    except Exception as exc:
        return f"<failed: {exc}>"


def _diag(phase: str) -> str:
    lines = [f"\n{'='*72}", f"DIAGNOSTICS -- {phase}", f"{'='*72}"]
    lines.append(f"\n--- {_ADMIN_CONTAINER} ---")
    lines.append(_container_logs(_ADMIN_CONTAINER, tail=100))
    lines.append("=" * 72)
    return "\n".join(lines)


def _wait_for_tui_chat(deadline_seconds: int = 300) -> tuple[str | None, str]:
    """Poll chat via ``bun run src/index.ts run`` until it succeeds."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        output, rc = _tui_run_chat(timeout=120)
        if rc == 0 and output:
            return output, "ok"
        logger.info("TUI chat probe: rc=%d -- retrying in 10s", rc)
        time.sleep(10)
    return None, "timeout"


def _purge_soft_deleted_resources() -> None:
    try:
        r = _run(
            ["az", "cognitiveservices", "account", "list-deleted", "-o", "json"],
            check=False, timeout=30,
        )
        if r.returncode != 0:
            return
        deleted = json.loads(r.stdout) if r.stdout.strip() else []
        for item in deleted:
            name = item.get("name", "")
            loc = item.get("location", "")
            res_id = item.get("id", "")
            rg = ""
            if "/resourceGroups/" in res_id:
                rg = res_id.split("/resourceGroups/")[1].split("/")[0]
            if _BASE_NAME in name or rg == _RG:
                logger.info("Purging soft-deleted: %s (rg=%s)", name, rg)
                _run(
                    ["az", "cognitiveservices", "account", "purge",
                     "--name", name, "--resource-group", rg, "--location", loc],
                    check=False, timeout=60,
                )
    except Exception as exc:
        logger.warning("Soft-delete purge failed: %s", exc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _ensure_bun():
    try:
        _run(["bun", "--version"], timeout=10)
    except Exception:
        pytest.skip("Bun is not installed -- required for TUI headless tests")


@pytest.fixture(scope="module")
def _ensure_tui_deps(_ensure_bun):
    if not (_TUI_DIR / "node_modules").exists():
        logger.info("Installing TUI dependencies ...")
        _run(["bun", "install"], cwd=_TUI_DIR, timeout=60)


@pytest.fixture(scope="module")
def aca_setup(_ensure_tui_deps):
    """Run ``bun run src/index.ts aca-setup`` -- full ACA provisioning.

    Builds images, starts local admin, deploys Foundry + ACA,
    verifies the first chat probe works through the admin proxy.
    """
    try:
        _run(["docker", "info"], timeout=15)
    except Exception:
        pytest.skip("Docker not available")

    # Clean slate -- stop any local containers
    _compose("down", "-v", "--remove-orphans", timeout=60)

    logger.info("Running TUI ACA headless setup ...")
    r = _tui(
        "aca-setup",
        timeout=2700,  # 45 min max
        check=False,
        extra_env=_aca_setup_env(),
    )

    for line in (r.stdout + r.stderr).splitlines():
        if line.strip():
            logger.info("[tui:aca-setup] %s", line.rstrip())

    if r.returncode != 0:
        pytest.fail(
            f"TUI ACA setup failed (exit {r.returncode}).\n"
            f"stdout:\n{r.stdout[-3000:]}\n"
            f"stderr:\n{r.stderr[-3000:]}\n"
            f"{_diag('aca-setup')}"
        )

    # Parse structured JSON result
    result = {}
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            result = json.loads(line)
            break
        except (json.JSONDecodeError, ValueError):
            continue

    assert result.get("status") == "ok", f"ACA setup did not return ok: {result}"
    logger.info("ACA setup complete: %s", json.dumps(result))

    yield result

    # Teardown -- stop admin, ACA resources cleaned in _cleanup_azure
    logger.info("Stopping admin container ...")
    _compose("down", "-v", "--remove-orphans", timeout=60)


@pytest.fixture(scope="module", autouse=True)
def _cleanup_azure():
    _purge_soft_deleted_resources()
    yield
    logger.info("Cleaning up Azure RG %s ...", _RG)
    try:
        _run(["az", "group", "delete", "--name", _RG, "--yes", "--no-wait"],
             check=False, timeout=30)
    except Exception as exc:
        logger.warning("RG cleanup failed: %s", exc)


# ===================================================================
# Tests
# ===================================================================


@pytest.mark.e2e_setup
class TestAcaLifecycle01Setup:
    """ACA headless setup: build, Foundry + ACA deploy, first inference."""

    def test_setup_completed(self, aca_setup) -> None:
        assert aca_setup["status"] == "ok"
        assert aca_setup.get("target") == "aca"
        logger.info("ACA setup OK in %ss", aca_setup.get("elapsed_seconds", "?"))

    def test_first_chat_via_tui(self, aca_setup) -> None:
        probe = aca_setup.get("probe_response", "")
        assert probe, "ACA setup did not return a chat probe response"
        logger.info("First inference (from ACA setup): %s", probe[:200])

    def test_admin_running_locally(self, aca_setup) -> None:
        """Admin container must be running locally."""
        r = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", _ADMIN_CONTAINER],
            check=False, timeout=10,
        )
        assert r.stdout.strip() == "true", (
            f"Admin container not running: {r.stdout} {r.stderr}"
        )

    def test_aca_status(self, aca_setup) -> None:
        """ACA status endpoint must show deployment info."""
        r = _run(
            ["curl", "-sf", "--max-time", "10", f"{_ADMIN_URL}/api/setup/aca/status"],
            check=False, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            assert data.get("deployed"), f"ACA not marked as deployed: {data}"
            assert data.get("runtime_fqdn"), "No runtime FQDN in ACA status"
            logger.info("ACA status: fqdn=%s acr=%s", data["runtime_fqdn"], data.get("acr_name"))

    def test_sp_written_to_env(self, aca_setup) -> None:
        r = _run(
            ["docker", "exec", _ADMIN_CONTAINER, "cat", "/data/.env"],
            check=False, timeout=15,
        )
        assert r.returncode == 0, f"Could not read .env: {r.stderr}"
        env = {}
        for line in r.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
        assert env.get("FOUNDRY_ENDPOINT"), "FOUNDRY_ENDPOINT not in .env"
        assert env.get("ACA_RUNTIME_FQDN"), "ACA_RUNTIME_FQDN not in .env"
        assert env.get("RUNTIME_URL"), "RUNTIME_URL not in .env"


@pytest.mark.e2e_setup
class TestAcaLifecycle02AcaRestart:
    """Restart the ACA runtime revision -- chat must still work."""

    def test_aca_restart(self, aca_setup) -> None:
        """Trigger an ACA revision restart via the TUI."""
        r = _tui("aca-restart", timeout=120, check=False, extra_env=_aca_setup_env())
        for line in (r.stdout + r.stderr).splitlines():
            if line.strip():
                logger.info("[tui:aca-restart] %s", line.rstrip())
        assert r.returncode == 0, (
            f"TUI aca-restart failed: {r.stdout[-500:]} {r.stderr[-500:]}"
        )

    def test_chat_after_aca_restart(self, aca_setup) -> None:
        """Chat must work after ACA restart (cold start may take a minute)."""
        # ACA restart creates a new revision -- give it time
        time.sleep(30)
        text, status = _wait_for_tui_chat(deadline_seconds=300)
        if status != "ok":
            pytest.fail(
                f"TUI chat failed after ACA restart. status={status}\n"
                f"{_diag('aca-restart-chat')}"
            )
        assert text, "Chat returned empty response"
        logger.info("Post ACA-restart inference OK: %s", text[:200])


@pytest.mark.e2e_setup
class TestAcaLifecycle03AdminRestart:
    """Restart the local admin container -- ACA runtime stays up."""

    def test_admin_restart(self, aca_setup) -> None:
        r = _run(["docker", "restart", _ADMIN_CONTAINER], check=False, timeout=60)
        assert r.returncode == 0, f"docker restart admin failed: {r.stderr}"
        logger.info("Admin container restarted")

    def test_admin_healthy_after_restart(self, aca_setup) -> None:
        health = _poll_health(timeout=90)
        if health is None:
            pytest.fail(
                f"Admin not healthy after restart.\n{_diag('admin-restart')}"
            )
        assert health["status"] == "ok"

    def test_chat_after_admin_restart(self, aca_setup) -> None:
        """Chat must work -- admin reconnects to ACA runtime."""
        text, status = _wait_for_tui_chat(deadline_seconds=300)
        if status != "ok":
            pytest.fail(
                f"TUI chat failed after admin restart. status={status}\n"
                f"{_diag('admin-restart-chat')}"
            )
        assert text, "Chat returned empty response"
        logger.info("Post admin-restart inference OK: %s", text[:200])


@pytest.mark.e2e_setup
class TestAcaLifecycle04RandomRestarts:
    """Random restarts of admin container with pauses."""

    def test_random_admin_restarts(self, aca_setup) -> None:
        rounds = random.randint(2, 3)
        logger.info("Running %d random admin restarts ...", rounds)
        for i in range(rounds):
            pause = random.uniform(3, 10)
            logger.info("Admin restart %d/%d -- pausing %.1fs", i + 1, rounds, pause)
            time.sleep(pause)
            r = _run(["docker", "restart", _ADMIN_CONTAINER], check=False, timeout=60)
            assert r.returncode == 0, f"docker restart #{i + 1} failed: {r.stderr}"
        logger.info("All %d admin restarts issued", rounds)

    def test_admin_healthy_after_random_restarts(self, aca_setup) -> None:
        health = _poll_health(timeout=120)
        if health is None:
            pytest.fail(
                f"Admin not healthy after random restarts.\n{_diag('random-restart')}"
            )
        assert health["status"] == "ok"

    def test_chat_after_random_restarts(self, aca_setup) -> None:
        text, status = _wait_for_tui_chat(deadline_seconds=300)
        if status != "ok":
            pytest.fail(
                f"TUI chat failed after random restarts. status={status}\n"
                f"{_diag('random-restart-chat')}"
            )
        assert text, "Chat returned empty response"
        logger.info("Post random-restart inference OK: %s", text[:200])


@pytest.mark.e2e_setup
class TestAcaLifecycle05Decommission:
    """Tear down ACA + Foundry resources via the TUI."""

    def test_decommission_via_tui(self, aca_setup) -> None:
        r = _tui(
            "aca-decommission",
            timeout=600,
            check=False,
            extra_env={"POLYCLAW_SETUP_RG": _RG},
        )
        for line in (r.stdout + r.stderr).splitlines():
            if line.strip():
                logger.info("[tui:aca-decommission] %s", line.rstrip())
        # Best-effort -- don't assert exit code
