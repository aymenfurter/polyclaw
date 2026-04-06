"""Docker restart survival integration test.

Verifies that the polyclaw container stack survives restarts without
losing healthy state.  Collects container logs and diagnostics when a
failure is detected to help pinpoint the root cause.

Usage:
    pytest app/runtime/tests/test_restart_survival.py --run-slow -s

Requires Docker to be running and the project root to contain a valid
docker-compose.yml.  The test tears down its stack on exit (even on
failure) so it does not leave containers lying around.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_FILE = _PROJECT_ROOT / "docker-compose.yml"
_HEALTH_URL_ADMIN = "http://localhost:9090/health"
_RUNTIME_HEALTH_INTERNAL = "http://localhost:8080/health"
_ADMIN_CONTAINER = "polyclaw-admin"
_RUNTIME_CONTAINER = "polyclaw-runtime"

# Timeouts
_BUILD_TIMEOUT = 600  # 10 min for image build
_BOOT_TIMEOUT = 120  # 2 min for containers to become healthy
_HEALTH_POLL_INTERVAL = 2  # seconds between health checks
_RESTART_SETTLE = 5  # seconds to wait after restart command
_CHAT_TIMEOUT = 90  # seconds to wait for a chat response

# A small Python script that runs *inside* the runtime container, opens a
# WebSocket to the local chat endpoint, sends a probe message, and prints
# the concatenated response.  Uses only stdlib + aiohttp (already installed
# in the container image).
#
# Exit codes:
#   0 -- got a response (printed to stdout)
#   2 -- agent not authenticated (prints error detail to stderr)
#   1 -- other failure
_CHAT_PROBE_SCRIPT = r"""
import asyncio, json, sys, os

import aiohttp

async def main():
    # Read the admin secret for authentication
    secret = ""
    try:
        with open("/data/.env") as f:
            for line in f:
                if line.startswith("ADMIN_SECRET="):
                    secret = line.split("=", 1)[1].strip().strip('"')
    except FileNotFoundError:
        pass

    # Connect directly to the local server (runtime listens on 8080)
    port = os.environ.get("ADMIN_PORT", "8080")
    url = f"http://localhost:{port}/api/chat/ws"
    headers = {}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    timeout = aiohttp.ClientTimeout(total=80)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(url, headers=headers) as ws:
            await ws.send_json({"action": "send", "text": "Reply with exactly: HEALTH_PROBE_OK"})
            chunks = []
            full_messages = []
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    t = data.get("type", "")
                    if t == "delta":
                        chunks.append(data.get("content", ""))
                    elif t == "message":
                        full_messages.append(data.get("content", ""))
                    elif t == "done":
                        break
                    elif t == "error":
                        content = data.get("content", "")
                        # Distinguish "not authenticated" from other errors
                        if "not authenticated" in content.lower() or "not respond" in content.lower():
                            print(content, file=sys.stderr)
                            sys.exit(2)
                        print("ERROR:" + content, file=sys.stderr)
                        sys.exit(1)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            response = "".join(chunks) or "\n".join(full_messages)
            print(response)

asyncio.run(main())
"""


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
    """Run a shell command and return the result."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        cwd=cwd or _PROJECT_ROOT,
    )


def _compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a ``docker compose`` command against the project root."""
    return _run(["docker", "compose", *args], timeout=timeout)


def _poll_health(url: str, timeout: float = _BOOT_TIMEOUT) -> dict[str, Any] | None:
    """Poll a health endpoint until it returns 200 or ``timeout`` expires.

    Returns the parsed JSON body on success, ``None`` on timeout.
    """
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            r = _run(
                ["curl", "-sf", "--max-time", "3", url],
                check=False,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
            last_error = str(exc)
        time.sleep(_HEALTH_POLL_INTERVAL)

    logger.warning("Health poll timed out for %s (last error: %s)", url, last_error)
    return None


def _poll_runtime_health(timeout: float = _BOOT_TIMEOUT) -> dict[str, Any] | None:
    """Poll the runtime health endpoint via ``docker exec``.

    The runtime container only exposes port 3978 (bot endpoint) to the
    host.  The web server on port 8080 is only reachable from inside the
    container or the Docker network.
    """
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            r = _run(
                [
                    "docker", "exec", _RUNTIME_CONTAINER,
                    "curl", "-sf", "--max-time", "3", _RUNTIME_HEALTH_INTERNAL,
                ],
                check=False,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
            last_error = str(exc)
        time.sleep(_HEALTH_POLL_INTERVAL)

    logger.warning("Runtime health poll timed out (last error: %s)", last_error)
    return None


def _send_chat_probe() -> tuple[str | None, str]:
    """Send a chat probe message via WebSocket and return the response.

    Runs a small Python script inside the **runtime** container that
    connects to the local WebSocket chat endpoint, sends a test prompt,
    and collects the streamed response.

    Returns ``(response_text, status)`` where status is one of:
      - ``"ok"`` -- got a response containing the expected marker
      - ``"not_authenticated"`` -- agent is not authenticated (GITHUB_TOKEN missing)
      - ``"empty"`` -- connected but got an empty response
      - ``"error"`` -- script failed or timed out
    """
    try:
        r = _run(
            [
                "docker", "exec", _RUNTIME_CONTAINER,
                "python", "-c", _CHAT_PROBE_SCRIPT,
            ],
            check=False,
            timeout=_CHAT_TIMEOUT,
        )
        if r.returncode == 2:
            # Agent not authenticated -- expected in Docker without GITHUB_TOKEN
            return None, "not_authenticated"
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip(), "ok"
        if r.returncode == 0:
            return None, "empty"
        logger.warning(
            "Chat probe failed (exit %d): stdout=%r stderr=%r",
            r.returncode, r.stdout[:200], r.stderr[:200],
        )
        return None, "error"
    except subprocess.TimeoutExpired:
        logger.warning("Chat probe timed out after %ds", _CHAT_TIMEOUT)
        return None, "error"
    except Exception as exc:
        logger.warning("Chat probe error: %s", exc)
        return None, "error"


def _container_logs(container: str, tail: int = 200) -> str:
    """Fetch the last ``tail`` lines of logs from a container."""
    try:
        r = _run(
            ["docker", "logs", "--tail", str(tail), container],
            check=False,
            timeout=15,
        )
        return (r.stdout + r.stderr).strip()
    except Exception as exc:
        return f"<failed to fetch logs: {exc}>"


def _container_inspect(container: str) -> dict[str, Any]:
    """Return the docker inspect JSON for a container."""
    try:
        r = _run(
            ["docker", "inspect", container],
            check=False,
            timeout=15,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            return data[0] if data else {}
    except Exception:
        pass
    return {}


def _collect_diagnostics(phase: str) -> dict[str, Any]:
    """Gather container state and logs for failure analysis."""
    diag: dict[str, Any] = {"phase": phase, "timestamp": time.time()}

    for name in (_ADMIN_CONTAINER, _RUNTIME_CONTAINER):
        info = _container_inspect(name)
        state = info.get("State", {})
        diag[name] = {
            "status": state.get("Status", "unknown"),
            "running": state.get("Running", False),
            "exit_code": state.get("ExitCode", -1),
            "oom_killed": state.get("OOMKilled", False),
            "restart_count": info.get("RestartCount", 0),
            "started_at": state.get("StartedAt", ""),
            "finished_at": state.get("FinishedAt", ""),
            "health": state.get("Health", {}).get("Status", "none"),
            "logs_tail": _container_logs(name, tail=80),
        }

    return diag


def _format_diagnostics(diag: dict[str, Any]) -> str:
    """Render diagnostics as a human-readable report."""
    lines = [
        f"\n{'='*72}",
        f"RESTART SURVIVAL DIAGNOSTICS -- phase: {diag['phase']}",
        f"{'='*72}",
    ]
    for name in (_ADMIN_CONTAINER, _RUNTIME_CONTAINER):
        info = diag.get(name, {})
        lines.append(f"\n--- {name} ---")
        lines.append(f"  status:        {info.get('status')}")
        lines.append(f"  running:       {info.get('running')}")
        lines.append(f"  exit_code:     {info.get('exit_code')}")
        lines.append(f"  oom_killed:    {info.get('oom_killed')}")
        lines.append(f"  restart_count: {info.get('restart_count')}")
        lines.append(f"  health:        {info.get('health')}")
        lines.append(f"  started_at:    {info.get('started_at')}")
        lines.append(f"  finished_at:   {info.get('finished_at')}")
        logs = info.get("logs_tail", "")
        if logs:
            lines.append(f"  logs (last 80 lines):")
            for log_line in logs.splitlines():
                lines.append(f"    {log_line}")
    lines.append(f"{'='*72}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _docker_available() -> None:
    """Skip the entire module if Docker is not available."""
    try:
        r = _run(["docker", "info"], check=False, timeout=10)
        if r.returncode != 0:
            pytest.skip("Docker daemon not available")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("Docker CLI not found or timed out")

    if not _COMPOSE_FILE.exists():
        pytest.skip(f"docker-compose.yml not found at {_COMPOSE_FILE}")


@pytest.fixture(scope="module")
def compose_stack(_docker_available: None) -> str:
    """Build and start the compose stack; tear it down after all tests."""
    # Tear down any pre-existing stack so we start clean
    _compose("down", "--remove-orphans", timeout=60)

    # Build
    try:
        _compose("build", timeout=_BUILD_TIMEOUT)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"docker compose build failed:\n{exc.stderr}")

    # Start -- use a longer timeout because `docker compose up -d` blocks
    # until healthcheck-dependent containers report healthy.
    try:
        _compose("up", "-d", "--wait", timeout=_BOOT_TIMEOUT + 30)
    except subprocess.CalledProcessError as exc:
        # Collect diagnostics before tearing down
        diag = _collect_diagnostics("compose_up")
        _compose("down", "--remove-orphans", timeout=30)
        pytest.fail(
            f"docker compose up failed:\n{exc.stderr}"
            + _format_diagnostics(diag)
        )

    yield _ADMIN_CONTAINER

    # Teardown -- always run
    _compose("down", "--remove-orphans", timeout=60)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestRestartSurvival:
    """Verify the container stack survives stop/start and restart cycles."""

    def test_initial_boot_healthy(self, compose_stack: str) -> None:
        """After first ``docker compose up``, both containers become healthy."""
        admin_health = _poll_health(_HEALTH_URL_ADMIN)
        if admin_health is None:
            diag = _collect_diagnostics("initial_boot_admin")
            pytest.fail(
                f"Admin container did not become healthy within {_BOOT_TIMEOUT}s."
                + _format_diagnostics(diag)
            )
        assert admin_health["status"] == "ok"

        runtime_health = _poll_runtime_health()
        if runtime_health is None:
            diag = _collect_diagnostics("initial_boot_runtime")
            pytest.fail(
                f"Runtime container did not become healthy within {_BOOT_TIMEOUT}s."
                + _format_diagnostics(diag)
            )
        assert runtime_health["status"] == "ok"

    def test_chat_works_after_boot(self, compose_stack: str) -> None:
        """The chat WebSocket accepts a prompt and returns a response.

        If the Copilot CLI is not authenticated (no GITHUB_TOKEN in the
        container), the test still passes because the WebSocket pipeline
        itself is functional -- only the upstream model is unreachable.
        """
        # Runtime must be up first
        if _poll_runtime_health(timeout=30) is None:
            pytest.skip("Runtime not healthy -- cannot test chat")

        response, status = _send_chat_probe()

        if status == "not_authenticated":
            # WebSocket worked, agent processed the message, but Copilot
            # CLI has no token.  This is not a restart failure.
            logger.info("Chat probe: agent not authenticated (expected in test)")
            return

        if status == "error":
            diag = _collect_diagnostics("chat_after_boot")
            pytest.fail(
                "Chat probe failed (WebSocket unreachable or script error) after initial boot."
                + _format_diagnostics(diag)
            )

        if status == "empty":
            diag = _collect_diagnostics("chat_after_boot_empty")
            pytest.fail(
                "Chat probe connected but got empty response after initial boot."
                + _format_diagnostics(diag)
            )

        assert response is not None
        assert "HEALTH_PROBE_OK" in response, (
            f"Chat response did not contain expected marker: {response[:200]}"
        )

    def test_restart_admin_survives(self, compose_stack: str) -> None:
        """Restarting the admin container recovers to healthy state."""
        # Ensure we start from a healthy baseline
        baseline = _poll_health(_HEALTH_URL_ADMIN)
        if baseline is None:
            pytest.skip("Admin not healthy before restart test")

        _compose("restart", "admin")
        time.sleep(_RESTART_SETTLE)

        health = _poll_health(_HEALTH_URL_ADMIN)
        if health is None:
            diag = _collect_diagnostics("restart_admin")
            pytest.fail(
                "Admin container did not recover after restart."
                + _format_diagnostics(diag)
            )
        assert health["status"] == "ok"

    def test_restart_runtime_survives(self, compose_stack: str) -> None:
        """Restarting the runtime container recovers to healthy state."""
        baseline = _poll_runtime_health()
        if baseline is None:
            pytest.skip("Runtime not healthy before restart test")

        _compose("restart", "runtime")
        time.sleep(_RESTART_SETTLE)

        health = _poll_runtime_health()
        if health is None:
            diag = _collect_diagnostics("restart_runtime")
            pytest.fail(
                "Runtime container did not recover after restart."
                + _format_diagnostics(diag)
            )
        assert health["status"] == "ok"

    def test_full_stack_restart_survives(self, compose_stack: str) -> None:
        """Full ``docker compose restart`` recovers both containers."""
        _compose("restart")
        time.sleep(_RESTART_SETTLE)

        admin_health = _poll_health(_HEALTH_URL_ADMIN)
        runtime_health = _poll_runtime_health()

        failures: list[str] = []
        if admin_health is None:
            failures.append("admin did not recover")
        elif admin_health["status"] != "ok":
            failures.append(f"admin status={admin_health['status']}")

        if runtime_health is None:
            failures.append("runtime did not recover")
        elif runtime_health["status"] != "ok":
            failures.append(f"runtime status={runtime_health['status']}")

        if failures:
            diag = _collect_diagnostics("full_stack_restart")
            pytest.fail(
                f"Full stack restart failed: {', '.join(failures)}"
                + _format_diagnostics(diag)
            )

    def test_chat_works_after_restart(self, compose_stack: str) -> None:
        """Chat still works after a full stack restart.

        Same tolerance as ``test_chat_works_after_boot``: if the agent
        is not authenticated the test passes because the WebSocket
        pipeline itself survived the restart.
        """
        if _poll_runtime_health(timeout=30) is None:
            pytest.skip("Runtime not healthy after restart -- cannot test chat")

        response, status = _send_chat_probe()

        if status == "not_authenticated":
            logger.info("Chat probe post-restart: agent not authenticated (expected)")
            return

        if status == "error":
            diag = _collect_diagnostics("chat_after_restart")
            pytest.fail(
                "Chat probe failed (WebSocket unreachable or script error) after restart."
                + _format_diagnostics(diag)
            )

        if status == "empty":
            diag = _collect_diagnostics("chat_after_restart_empty")
            pytest.fail(
                "Chat probe connected but got empty response after restart."
                + _format_diagnostics(diag)
            )

        assert response is not None
        assert "HEALTH_PROBE_OK" in response, (
            f"Chat response did not contain expected marker: {response[:200]}"
        )

    def test_stop_start_cycle_survives(self, compose_stack: str) -> None:
        """``docker compose stop`` then ``up -d`` recovers cleanly."""
        _compose("stop", timeout=30)
        time.sleep(2)

        # Verify containers are actually stopped
        for name in (_ADMIN_CONTAINER, _RUNTIME_CONTAINER):
            info = _container_inspect(name)
            state = info.get("State", {})
            assert not state.get("Running", True), f"{name} still running after stop"

        _compose("up", "-d")
        time.sleep(_RESTART_SETTLE)

        admin_health = _poll_health(_HEALTH_URL_ADMIN)
        if admin_health is None:
            diag = _collect_diagnostics("stop_start_admin")
            pytest.fail(
                "Admin did not recover after stop/start cycle."
                + _format_diagnostics(diag)
            )
        assert admin_health["status"] == "ok"

        runtime_health = _poll_runtime_health()
        if runtime_health is None:
            diag = _collect_diagnostics("stop_start_runtime")
            pytest.fail(
                "Runtime did not recover after stop/start cycle."
                + _format_diagnostics(diag)
            )
        assert runtime_health["status"] == "ok"

    def test_rapid_restart_cycle(self, compose_stack: str) -> None:
        """Three rapid restarts in succession do not corrupt state."""
        for i in range(3):
            _compose("restart")
            time.sleep(_RESTART_SETTLE)

        admin_health = _poll_health(_HEALTH_URL_ADMIN)
        runtime_health = _poll_runtime_health()

        if admin_health is None or runtime_health is None:
            diag = _collect_diagnostics("rapid_restart")
            unhealthy = []
            if admin_health is None:
                unhealthy.append("admin")
            if runtime_health is None:
                unhealthy.append("runtime")
            pytest.fail(
                f"Containers unhealthy after 3 rapid restarts: {', '.join(unhealthy)}"
                + _format_diagnostics(diag)
            )

        assert admin_health["status"] == "ok"
        assert runtime_health["status"] == "ok"

    def test_state_files_survive_restart(self, compose_stack: str) -> None:
        """State files on the shared volume are not corrupted by restarts.

        Writes a marker file, restarts, then verifies the marker is intact.
        """
        marker = {"test": "restart_survival", "ts": time.time()}
        marker_json = json.dumps(marker)

        # Write marker into the shared data volume via docker exec
        r = _run(
            [
                "docker", "exec", _ADMIN_CONTAINER,
                "python", "-c",
                f"import pathlib; pathlib.Path('/data/.restart_test_marker.json').write_text('{marker_json}')",
            ],
            check=False,
        )
        if r.returncode != 0:
            pytest.skip(f"Could not write marker file: {r.stderr}")

        _compose("restart")
        time.sleep(_RESTART_SETTLE)
        _poll_health(_HEALTH_URL_ADMIN, timeout=60)

        # Read marker back
        r = _run(
            [
                "docker", "exec", _ADMIN_CONTAINER,
                "cat", "/data/.restart_test_marker.json",
            ],
            check=False,
        )
        if r.returncode != 0:
            diag = _collect_diagnostics("state_file_read")
            pytest.fail(
                "Could not read marker file after restart."
                + _format_diagnostics(diag)
            )

        recovered = json.loads(r.stdout)
        assert recovered == marker, f"Marker mismatch: {recovered} != {marker}"

        # Cleanup
        _run(
            [
                "docker", "exec", _ADMIN_CONTAINER,
                "rm", "-f", "/data/.restart_test_marker.json",
            ],
            check=False,
        )
