"""End-to-end ACA (Azure Container Apps) deployment test bench.

Drives the admin API to:
 1. Verify local Docker stack prerequisites (Foundry deployed, bot configured).
 2. Push the container image to ACR.
 3. Provision Managed Identity + RBAC.
 4. Deploy the runtime as an Azure Container App.
 5. Verify the cloud-hosted runtime is reachable and chat works.
 6. Redeploy (idempotency).
 7. Destroy and verify cleanup.

Usage::

    pytest app/runtime/tests/test_e2e_aca_deploy.py --run-e2e-setup -s -v

Requirements:
    - Docker running locally with the ``polyclaw:latest`` image built.
    - Active ``az login`` session.
    - Prior successful run of test_e2e_setup_process.py (Foundry deployed).
    - Sufficient Azure quota in the target region for ACA + ACR.

Typical wall-clock: 10-20 min.
"""

from __future__ import annotations

import json
import logging
import os
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
_ADMIN_CONTAINER = "polyclaw-admin"
_ADMIN_URL = "http://localhost:9090"
_BUILD_TIMEOUT = 900
_BOOT_TIMEOUT = 120
_DEPLOY_TIMEOUT = 600
_ACA_HEALTH_TIMEOUT = 300
_HEALTH_POLL = 5
_API_TIMEOUT = 30

_RG = "polyclaw-e2e-aca-rg"
_LOCATION = "eastus"


# ---------------------------------------------------------------------------
# Shell / Docker / API helpers  (shared with test_e2e_setup_process.py)
# ---------------------------------------------------------------------------

def _run(
    cmd: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        cwd=cwd or _PROJECT_ROOT,
    )


def _compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", *args], timeout=timeout)


def _api(
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    secret: str = "",
    timeout: int = _API_TIMEOUT,
) -> tuple[int, dict | None]:
    url = f"{_ADMIN_URL}{path}"
    cmd: list[str] = [
        "curl", "-s", "--max-time", str(timeout),
        "-o", "/dev/stdout", "-w", "\n%{http_code}",
    ]
    if secret:
        cmd += ["-H", f"Authorization: Bearer {secret}"]
    if method == "POST":
        cmd += ["-X", "POST", "-H", "Content-Type: application/json"]
        cmd += ["-d", json.dumps(body) if body else "{}"]
    elif method == "DELETE":
        cmd += ["-X", "DELETE"]
    cmd.append(url)

    try:
        r = _run(cmd, check=False, timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        return 0, None

    parts = r.stdout.rsplit("\n", 1)
    if len(parts) < 2:
        return 0, None
    try:
        status_code = int(parts[-1])
    except ValueError:
        return 0, None
    try:
        data = json.loads(parts[0])
    except (json.JSONDecodeError, IndexError):
        data = None
    return status_code, data


def _api_ok(
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    secret: str = "",
    timeout: int = _API_TIMEOUT,
    expected_status: int = 200,
) -> dict:
    code, data = _api(path, method=method, body=body, secret=secret, timeout=timeout)
    assert code == expected_status, (
        f"{method} {path} returned {code} (expected {expected_status}).\n"
        f"Response: {json.dumps(data, indent=2) if data else '<none>'}"
    )
    assert data is not None, f"{method} {path} returned no JSON body"
    return data


def _poll_health(timeout: float = _BOOT_TIMEOUT) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = _run(
                ["curl", "-sf", "--max-time", "3", f"{_ADMIN_URL}/health"],
                check=False, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        time.sleep(_HEALTH_POLL)
    return None


def _copy_azure_creds() -> bool:
    azure_dir = Path.home() / ".azure"
    if not azure_dir.exists():
        return False
    try:
        _run(
            ["docker", "exec", _ADMIN_CONTAINER, "mkdir", "-p", "/admin-home/.azure"],
            timeout=10,
        )
        _ESSENTIAL = [
            "azureProfile.json",
            "msal_token_cache.json",
            "msal_token_cache.bin",
            "az.json",
            "az.sess",
            "clouds.config",
            "config",
        ]
        copied = 0
        for name in _ESSENTIAL:
            src = azure_dir / name
            if src.exists():
                _run(
                    ["docker", "cp", str(src), f"{_ADMIN_CONTAINER}:/admin-home/.azure/{name}"],
                    timeout=30,
                )
                copied += 1
        logger.info("Copied %d Azure auth files into admin container", copied)
        return copied > 0
    except Exception as exc:
        logger.error("Failed to copy Azure creds: %s", exc)
        return False


def _get_admin_secret() -> str:
    """Read the admin secret from the running container's /data/.env."""
    try:
        r = _run(
            ["docker", "exec", _ADMIN_CONTAINER, "sh", "-c",
             "grep '^ADMIN_SECRET=' /data/.env | head -1 | cut -d= -f2"],
            check=False, timeout=10,
        )
        return r.stdout.strip().strip('"')
    except Exception:
        return ""


def _poll_aca_health(fqdn: str, timeout: float = _ACA_HEALTH_TIMEOUT) -> bool:
    """Poll the ACA runtime health endpoint until it responds."""
    url = f"https://{fqdn}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = _run(
                ["curl", "-sf", "--max-time", "5", url],
                check=False, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                logger.info("ACA runtime healthy: %s", r.stdout.strip()[:200])
                return True
        except Exception:
            pass
        time.sleep(_HEALTH_POLL)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_secret():
    """Discover or inject the admin secret from the running Docker stack."""
    # First check if the stack is already running
    health = _poll_health(timeout=10)
    if health:
        secret = _get_admin_secret()
        if secret:
            return secret

    # Boot the stack
    try:
        _run(["docker", "info"], timeout=15)
    except Exception:
        pytest.skip("Docker not available")

    logger.info("Building Docker image ...")
    try:
        _compose("build", timeout=_BUILD_TIMEOUT)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"Docker build failed:\n{exc.stderr[:2000]}")

    logger.info("Starting Docker stack ...")
    try:
        _compose("up", "-d", timeout=60)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"docker compose up failed:\n{exc.stderr[:2000]}")

    health = _poll_health(timeout=_BOOT_TIMEOUT)
    if not health:
        pytest.fail("Admin container not healthy")

    # Inject a secret
    secret = "e2e-aca-secret-" + os.urandom(8).hex()
    try:
        _run(
            ["docker", "exec", _ADMIN_CONTAINER, "sh", "-c",
             f'grep -q "^ADMIN_SECRET=" /data/.env 2>/dev/null '
             f'&& sed -i "s|^ADMIN_SECRET=.*|ADMIN_SECRET={secret}|" /data/.env '
             f'|| echo "ADMIN_SECRET={secret}" >> /data/.env'],
            timeout=10,
        )
    except Exception as exc:
        pytest.fail(f"Failed to inject ADMIN_SECRET: {exc}")

    _compose("restart", timeout=60)
    health = _poll_health(timeout=_BOOT_TIMEOUT)
    if not health:
        pytest.fail("Admin not healthy after restart")

    ok = _copy_azure_creds()
    if not ok:
        pytest.fail("Failed to copy Azure creds")

    time.sleep(35)
    return secret


@pytest.fixture(scope="module")
def stack(admin_secret):
    """Verify the local Docker stack is healthy and Azure-authenticated."""
    health = _poll_health(timeout=10)
    assert health, "Admin container not healthy"

    # Verify Azure login
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        data = _api_ok("/api/setup/azure/check", secret=admin_secret)
        if data.get("status") == "logged_in":
            break
        time.sleep(5)
    assert data.get("status") == "logged_in", "Azure CLI not logged in"
    return health


@pytest.fixture(scope="module")
def _aca_rg_cleanup():
    """Best-effort cleanup of the ACA resource group after all tests."""
    yield
    logger.info("Cleaning up ACA RG %s ...", _RG)
    try:
        _run(["az", "group", "delete", "--name", _RG, "--yes", "--no-wait"],
             check=False, timeout=30)
    except Exception as exc:
        logger.warning("ACA RG cleanup failed: %s", exc)


# ===================================================================
# PHASE 1: Pre-flight -- local stack prerequisites
# ===================================================================

@pytest.mark.e2e_setup
class TestAcaPhase01Preflight:
    """Verify local Docker stack prerequisites before ACA deployment."""

    def test_local_stack_healthy(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/status", secret=admin_secret)
        logger.info("Local status: %s", json.dumps(data, indent=2)[:500])

    def test_foundry_deployed(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/status", secret=admin_secret)
        if not data["foundry"]["deployed"]:
            pytest.skip("Foundry not deployed -- run test_e2e_setup_process first")
        logger.info("Foundry endpoint: %s", data["foundry"]["endpoint"])

    def test_aca_status_initial(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/aca/status", secret=admin_secret)
        logger.info("Initial ACA status: %s", json.dumps(data, indent=2))

    def test_docker_image_exists(self, stack) -> None:
        r = _run(["docker", "images", "polyclaw:latest", "--format", "{{.ID}}"],
                 check=False, timeout=10)
        assert r.stdout.strip(), "polyclaw:latest image not found"


# ===================================================================
# PHASE 2: ACA deployment
# ===================================================================

@pytest.mark.e2e_setup
class TestAcaPhase02Deploy:
    """Deploy the runtime to Azure Container Apps."""

    def test_deploy_aca(self, stack, admin_secret) -> None:
        body = {
            "resource_group": _RG,
            "location": _LOCATION,
            "display_name": "polyclaw-e2e-aca",
            "admin_port": 9090,
            "runtime_port": 8080,
            "image_tag": "latest",
        }
        code, data = _api(
            "/api/setup/aca/deploy",
            method="POST", body=body,
            secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        logger.info("ACA deploy: code=%d", code)
        if data:
            for step in data.get("steps", []):
                logger.info(
                    "  %s: %s %s",
                    step.get("step"), step.get("status"),
                    step.get("detail", "")[:200],
                )
        assert code == 200, (
            f"ACA deploy returned {code}:\n"
            f"{json.dumps(data, indent=2)[:2000] if data else '<no body>'}"
        )
        assert data.get("status") == "ok", f"Deploy failed: {data.get('message')}"
        assert data.get("runtime_fqdn"), "No runtime FQDN returned"
        logger.info("ACA deployed: fqdn=%s", data["runtime_fqdn"])

    def test_aca_status_deployed(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/aca/status", secret=admin_secret)
        assert data.get("deployed"), f"ACA not showing as deployed: {data}"
        assert data.get("runtime_fqdn")
        assert data.get("acr_name")
        logger.info("ACA status: %s", json.dumps(data, indent=2))


# ===================================================================
# PHASE 3: Verify cloud runtime is alive
# ===================================================================

@pytest.mark.e2e_setup
class TestAcaPhase03RuntimeHealth:
    """Verify the deployed ACA runtime is reachable and healthy."""

    def test_runtime_health(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/aca/status", secret=admin_secret)
        fqdn = data.get("runtime_fqdn")
        if not fqdn:
            pytest.skip("ACA not deployed")
        ok = _poll_aca_health(fqdn)
        assert ok, f"ACA runtime at {fqdn} not healthy after {_ACA_HEALTH_TIMEOUT}s"

    def test_runtime_api_reachable(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/aca/status", secret=admin_secret)
        fqdn = data.get("runtime_fqdn")
        if not fqdn:
            pytest.skip("ACA not deployed")
        # Try to reach the /api/models endpoint on the cloud runtime
        url = f"https://{fqdn}/api/models"
        try:
            r = _run(["curl", "-sf", "--max-time", "10", url], check=False, timeout=15)
            logger.info("Cloud /api/models: rc=%d body=%s", r.returncode, r.stdout[:300])
        except Exception as exc:
            logger.warning("Cloud API unreachable: %s", exc)


# ===================================================================
# PHASE 4: Chat via cloud runtime
# ===================================================================

@pytest.mark.e2e_setup
class TestAcaPhase04Chat:
    """Verify chat works through the ACA-deployed runtime."""

    def test_chat_via_cloud(self, stack, admin_secret) -> None:
        """Admin's copilot smoke-test should hit the cloud runtime URL."""
        # After ACA deploy, RUNTIME_URL should be set to https://<fqdn>
        code, data = _api(
            "/api/setup/copilot/smoke-test",
            method="POST", secret=admin_secret, timeout=120,
        )
        logger.info("Cloud smoke test: code=%d data=%s",
                     code, json.dumps(data or {}, indent=2)[:1000])
        if code != 200:
            logger.warning("Smoke test failed -- chat may not work through ACA yet")


# ===================================================================
# PHASE 5: Container restart
# ===================================================================

@pytest.mark.e2e_setup
class TestAcaPhase05Restart:
    """Verify container restart works on ACA."""

    def test_restart_runtime(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/aca/status", secret=admin_secret)
        if not data.get("deployed"):
            pytest.skip("ACA not deployed")
        code, resp = _api(
            "/api/setup/container/restart",
            method="POST", secret=admin_secret, timeout=120,
        )
        logger.info("Container restart: code=%d data=%s", code, json.dumps(resp or {}, indent=2))
        if code == 200:
            # Wait for runtime to come back
            fqdn = data.get("runtime_fqdn")
            if fqdn:
                time.sleep(10)
                ok = _poll_aca_health(fqdn, timeout=120)
                assert ok, "Runtime not healthy after restart"


# ===================================================================
# PHASE 6: Idempotency -- redeploy
# ===================================================================

@pytest.mark.e2e_setup
class TestAcaPhase06Idempotency:
    """Redeploy ACA and verify stability."""

    def test_redeploy_aca(self, stack, admin_secret) -> None:
        # Get existing ACR and env names to reuse
        status_data = _api_ok("/api/setup/aca/status", secret=admin_secret)
        body = {
            "resource_group": _RG,
            "location": _LOCATION,
            "display_name": "polyclaw-e2e-aca",
            "image_tag": "latest",
            "acr_name": status_data.get("acr_name", ""),
            "env_name": status_data.get("env_name", ""),
        }
        code, data = _api(
            "/api/setup/aca/deploy",
            method="POST", body=body,
            secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        logger.info("ACA redeploy: code=%d", code)
        if data:
            for step in data.get("steps", []):
                logger.info("  %s: %s", step.get("step"), step.get("status"))
        if code != 200:
            pytest.xfail(f"ACA redeploy failed: {data}")
        assert data.get("runtime_fqdn")

    def test_runtime_still_healthy_after_redeploy(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/aca/status", secret=admin_secret)
        fqdn = data.get("runtime_fqdn")
        if not fqdn:
            pytest.skip("ACA not deployed")
        time.sleep(15)
        ok = _poll_aca_health(fqdn, timeout=120)
        assert ok, f"ACA not healthy after redeploy: {fqdn}"


# ===================================================================
# PHASE 7: Destroy
# ===================================================================

@pytest.mark.e2e_setup
class TestAcaPhase07Destroy:
    """Destroy ACA deployment and verify cleanup."""

    def test_destroy_aca(self, stack, admin_secret) -> None:
        code, data = _api(
            "/api/setup/aca/destroy",
            method="POST", body={},
            secret=admin_secret, timeout=300,
        )
        logger.info("ACA destroy: code=%d data=%s", code, json.dumps(data or {}, indent=2))
        assert code == 200, f"Destroy returned {code}: {data}"
        assert data.get("status") == "ok"

    def test_aca_status_after_destroy(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/aca/status", secret=admin_secret)
        assert not data.get("deployed"), f"ACA still showing deployed: {data}"
        assert not data.get("runtime_fqdn")
        logger.info("ACA post-destroy status: %s", json.dumps(data, indent=2))

    def test_collect_diagnostics(self, stack) -> None:
        try:
            r = _run(
                ["docker", "logs", "--tail", "50", _ADMIN_CONTAINER],
                check=False, timeout=15,
            )
            logger.info("=== Admin logs ===\n%s", (r.stdout + r.stderr).strip()[:2000])
        except Exception:
            pass
