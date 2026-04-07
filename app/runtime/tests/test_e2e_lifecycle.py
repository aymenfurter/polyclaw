"""End-to-end lifecycle test via the TUI headless mode.

Uses the TUI's ``setup``, ``run``, ``health``, and ``stop`` CLI modes
so the test exercises the same code path a real user would follow.

  1. ``bun run src/index.ts setup`` -- build, start, Azure check, Foundry
     deploy, wait for BYOK, chat probe.
  2. ``docker restart polyclaw-runtime`` + ``bun run src/index.ts run`` --
     verify chat survives a container restart.
  3. ``docker compose stop`` / ``up -d`` + ``bun run src/index.ts run`` --
     verify chat survives a full stop/start cycle.
  4. ``bun run src/index.ts decommission`` -- tear down Azure resources.

Usage::

    pytest app/runtime/tests/test_e2e_lifecycle.py --run-e2e-setup -s -v

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
_RUNTIME_CONTAINER = "polyclaw-runtime"
_ADMIN_URL = "http://localhost:9090"
_HEALTH_URL = f"{_ADMIN_URL}/health"

_BOOT_TIMEOUT = 120
_HEALTH_POLL = 3

_RG = "polyclaw-e2e-lifecycle-rg"
_LOCATION = "eastus"
_BASE_NAME = "lc" + os.urandom(3).hex()
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


def _tui_setup_env() -> dict[str, str]:
    """Environment variables for the TUI ``setup`` mode."""
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
            r = _run(["curl", "-sf", "--max-time", "3", _HEALTH_URL], check=False, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        time.sleep(_HEALTH_POLL)
    return None


def _poll_runtime_health(timeout: float = _BOOT_TIMEOUT) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = _run(
                ["docker", "exec", _RUNTIME_CONTAINER,
                 "curl", "-sf", "--max-time", "3", "http://localhost:8080/health"],
                check=False, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        time.sleep(_HEALTH_POLL)
    return None


def _wait_for_runtime_ready(timeout: float = 120) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = _container_logs(_RUNTIME_CONTAINER, tail=80)
        if "BYOK provider injected" in logs or "BYOK mode" in logs:
            logger.info("Runtime BYOK mode confirmed")
            return True
        time.sleep(5)
    return False


def _container_logs(container: str, tail: int = 200) -> str:
    try:
        r = _run(["docker", "logs", "--tail", str(tail), container], check=False, timeout=15)
        return (r.stdout + r.stderr).strip()
    except Exception as exc:
        return f"<failed: {exc}>"


def _diag(phase: str) -> str:
    lines = [f"\n{'='*72}", f"DIAGNOSTICS -- {phase}", f"{'='*72}"]
    for c in (_ADMIN_CONTAINER, _RUNTIME_CONTAINER):
        lines.append(f"\n--- {c} ---")
        lines.append(_container_logs(c, tail=100))
    lines.append("=" * 72)
    return "\n".join(lines)


def _wait_for_tui_chat(deadline_seconds: int = 180) -> tuple[str | None, str]:
    """Poll chat via ``bun run src/index.ts run`` until it succeeds."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        output, rc = _tui_run_chat(timeout=90)
        if rc == 0 and output:
            return output, "ok"
        logger.info("TUI chat probe: rc=%d -- retrying in 8s", rc)
        time.sleep(8)
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
    """Skip the entire module if Bun is not installed."""
    try:
        _run(["bun", "--version"], timeout=10)
    except Exception:
        pytest.skip("Bun is not installed -- required for TUI headless tests")


@pytest.fixture(scope="module")
def _ensure_tui_deps(_ensure_bun):
    """Install TUI dependencies if needed."""
    if not (_TUI_DIR / "node_modules").exists():
        logger.info("Installing TUI dependencies ...")
        _run(["bun", "install"], cwd=_TUI_DIR, timeout=60)


@pytest.fixture(scope="module")
def tui_setup(_ensure_tui_deps):
    """Run ``bun run src/index.ts setup`` -- the full headless provisioning.

    This replaces the old ``stack`` fixture that manually called Docker
    and curl.  The TUI handles: Docker build, compose up, Azure cred
    mount, subscription selection, Foundry deploy, runtime readiness
    poll, and the first chat probe.
    """
    try:
        _run(["docker", "info"], timeout=15)
    except Exception:
        pytest.skip("Docker not available")

    # Clean slate
    _compose("down", "-v", "--remove-orphans", timeout=60)

    logger.info("Running TUI headless setup (build + deploy + chat probe) ...")
    r = _tui(
        "setup",
        timeout=900,
        check=False,
        extra_env=_tui_setup_env(),
    )

    # Log all TUI output for visibility
    for line in (r.stdout + r.stderr).splitlines():
        if line.strip():
            logger.info("[tui:setup] %s", line.rstrip())

    if r.returncode != 0:
        pytest.fail(
            f"TUI setup failed (exit {r.returncode}).\n"
            f"stdout:\n{r.stdout[-2000:]}\n"
            f"stderr:\n{r.stderr[-2000:]}\n"
            f"{_diag('tui-setup')}"
        )

    # Parse structured result from the last line of stdout
    result = {}
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            result = json.loads(line)
            break
        except (json.JSONDecodeError, ValueError):
            continue

    assert result.get("status") == "ok", (
        f"TUI setup did not return ok: {result}"
    )
    logger.info("TUI setup complete: %s", json.dumps(result))

    yield result

    # Teardown
    logger.info("Tearing down ...")
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
# Tests -- ordered to run sequentially
# ===================================================================


@pytest.mark.e2e_setup
class TestLifecycle01TuiSetup:
    """TUI headless setup: build, deploy, first inference."""

    def test_setup_completed(self, tui_setup) -> None:
        """The TUI ``setup`` command must exit 0 with status=ok."""
        assert tui_setup["status"] == "ok"
        logger.info("TUI setup OK in %ss", tui_setup.get("elapsed_seconds", "?"))

    def test_first_chat_via_tui(self, tui_setup) -> None:
        """The setup command already ran a chat probe -- verify it worked."""
        probe = tui_setup.get("probe_response", "")
        assert probe, "TUI setup did not return a chat probe response"
        logger.info("First inference (from TUI setup): %s", probe[:200])

    def test_sp_written_to_env(self, tui_setup) -> None:
        """SP creds must be in /data/.env after deploy."""
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
        assert env.get("RUNTIME_SP_APP_ID"), "RUNTIME_SP_APP_ID not in .env"
        assert env.get("RUNTIME_SP_PASSWORD"), "RUNTIME_SP_PASSWORD not in .env"
        assert env.get("RUNTIME_SP_TENANT"), "RUNTIME_SP_TENANT not in .env"
        assert env.get("FOUNDRY_ENDPOINT"), "FOUNDRY_ENDPOINT not in .env"


@pytest.mark.e2e_setup
class TestLifecycle02RestartSurvival:
    """After ``docker restart``, chat via TUI ``run`` must still work."""

    def test_restart_runtime(self, tui_setup) -> None:
        r = _run(["docker", "restart", _RUNTIME_CONTAINER], check=False, timeout=60)
        assert r.returncode == 0, f"docker restart failed: {r.stderr}"
        logger.info("Runtime container restarted")

    def test_runtime_healthy_after_restart(self, tui_setup) -> None:
        health = _poll_runtime_health(timeout=90)
        if health is None:
            pytest.fail(
                f"Runtime not healthy after restart.\n{_diag('restart-health')}"
            )
        assert health["status"] == "ok"

    def test_runtime_identity_after_restart(self, tui_setup) -> None:
        ready = _wait_for_runtime_ready(timeout=90)
        if not ready:
            logs = _container_logs(_RUNTIME_CONTAINER, tail=200)
            pytest.fail(
                f"Runtime BYOK mode not confirmed after restart.\n"
                f"Logs (last 1000 chars):\n{logs[-1000:]}"
            )

    def test_chat_after_restart_via_tui(self, tui_setup) -> None:
        """Send chat probe using ``bun run src/index.ts run``."""
        text, status = _wait_for_tui_chat(deadline_seconds=180)
        if status != "ok":
            pytest.fail(
                f"TUI chat failed after restart. status={status}\n"
                f"{_diag('restart-chat')}"
            )
        assert text, "Chat returned empty response"
        logger.info("Post-restart inference OK (via TUI run): %s", text[:200])


@pytest.mark.e2e_setup
class TestLifecycle02bRandomRestarts:
    """Restart the runtime 2-3 times with random pauses in between."""

    def test_rapid_restarts(self, tui_setup) -> None:
        """Restart runtime 2-3 times with random 3-15s pauses, then verify chat."""
        rounds = random.randint(2, 3)
        logger.info("Running %d rapid restarts with random pauses ...", rounds)
        for i in range(rounds):
            pause = random.uniform(3, 15)
            logger.info("Restart %d/%d -- pausing %.1fs before restart", i + 1, rounds, pause)
            time.sleep(pause)
            r = _run(["docker", "restart", _RUNTIME_CONTAINER], check=False, timeout=60)
            assert r.returncode == 0, f"docker restart #{i + 1} failed: {r.stderr}"
        logger.info("All %d restarts issued", rounds)

    def test_healthy_after_rapid_restarts(self, tui_setup) -> None:
        health = _poll_runtime_health(timeout=120)
        if health is None:
            pytest.fail(
                f"Runtime not healthy after rapid restarts.\n{_diag('rapid-restart')}"
            )
        assert health["status"] == "ok"

    def test_byok_after_rapid_restarts(self, tui_setup) -> None:
        ready = _wait_for_runtime_ready(timeout=120)
        if not ready:
            logs = _container_logs(_RUNTIME_CONTAINER, tail=200)
            pytest.fail(
                f"BYOK not confirmed after rapid restarts.\n"
                f"Logs (last 1000 chars):\n{logs[-1000:]}"
            )

    def test_chat_after_rapid_restarts(self, tui_setup) -> None:
        text, status = _wait_for_tui_chat(deadline_seconds=180)
        if status != "ok":
            pytest.fail(
                f"TUI chat failed after rapid restarts. status={status}\n"
                f"{_diag('rapid-restart-chat')}"
            )
        assert text, "Chat returned empty response"
        logger.info("Post rapid-restart inference OK: %s", text[:200])


@pytest.mark.e2e_setup
class TestLifecycle03StopStartSurvival:
    """Stop the stack and start it again -- TUI health + chat must work."""

    def test_stop_start_cycle(self, tui_setup) -> None:
        _compose("stop", timeout=30)
        time.sleep(2)
        _compose("up", "-d", timeout=60)
        health = _poll_health(timeout=_BOOT_TIMEOUT)
        assert health is not None, (
            f"Admin not healthy after stop/start.\n{_diag('stop-start')}"
        )

    def test_health_via_tui(self, tui_setup) -> None:
        """TUI ``health`` command must succeed."""
        # Wait for stack to be fully up
        time.sleep(5)
        r = _tui("health", timeout=30, check=False)
        assert r.returncode == 0, (
            f"TUI health failed: {r.stdout} {r.stderr}"
        )
        logger.info("TUI health OK: %s", r.stdout[:200])

    def test_chat_after_stop_start_via_tui(self, tui_setup) -> None:
        text, status = _wait_for_tui_chat(deadline_seconds=180)
        if status != "ok":
            pytest.fail(
                f"TUI chat failed after stop/start. status={status}\n"
                f"{_diag('stop-start-chat')}"
            )
        assert text, "Chat returned empty response"
        logger.info("Post stop/start inference OK (via TUI run): %s", text[:200])


@pytest.mark.e2e_setup
class TestLifecycle04Decommission:
    """Tear down Azure resources via the TUI."""

    def test_decommission_via_tui(self, tui_setup) -> None:
        r = _tui(
            "decommission",
            timeout=480,
            check=False,
            extra_env={"POLYCLAW_SETUP_RG": _RG},
        )
        for line in (r.stdout + r.stderr).splitlines():
            if line.strip():
                logger.info("[tui:decommission] %s", line.rstrip())
        # Best-effort -- don't assert exit code
