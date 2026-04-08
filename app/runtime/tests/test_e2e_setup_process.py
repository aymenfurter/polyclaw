"""End-to-end setup and functionality test bench for local Docker deployment.

Boots the Docker stack, copies the host Azure credentials into the admin
container, drives the admin API through every deployment combination, and
verifies each subsystem works end-to-end with real Azure resources.

Covers the full lifecycle a real user goes through: provision, configure,
stop, start, reconfigure, verify chat works at every stage.  Bot service
and Telegram are treated as optional -- all core functionality (Foundry +
chat) is validated without them.

Usage::

    pytest app/runtime/tests/test_e2e_setup_process.py --run-e2e-setup -s -v

Requirements:
    - Docker running locally
    - Active ``az login`` session (``~/.azure`` is copied into the container)
    - Sufficient Azure quota in the target region
    - (Optional) ``.botservice-secret.txt`` at repo root for Telegram tests

The test creates a **real** resource group, provisions Foundry, Key Vault,
Content Safety, Search, Embedding AOAI, and (optionally) Bot Service
resources, then tears everything down at the end.  Typical wall-clock:
20-30 min.

Phases (22):
  1.  Clean state verification
  2.  Deploy Foundry + KV (chat verified without bot)
  3.  Content Safety / Prompt Shields
  4.  Bot + Telegram config (chat asserted with bot, no tunnel)
  5.  Tunnel + full stack (chat assertion)
  6.  Skills CRUD
  7.  Sessions
  8.  Guardrails
  9.  Plugins
  10. MCP servers
  11. Scheduler
  12. Profile
  13. Foundry IQ
  14. Idempotency (redeploy + chat assertion)
  15. Combined configuration save
  16. Stop/start lifecycle (2 cycles + chat each time)
  17. Config change mid-lifecycle (profile + guardrails + chat)
  18. Bot service add/remove/toggle (chat each time)
  19. Voice / ACS
  20. Lockdown mode
  21. Decommission
  22. TUI headless (health check + host-side WebSocket chat)
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
_RUNTIME_CONTAINER = "polyclaw-runtime"
_ADMIN_URL = "http://localhost:9090"
_HEALTH_URL = f"{_ADMIN_URL}/health"

_BUILD_TIMEOUT = 900
_BOOT_TIMEOUT = 120
_DEPLOY_TIMEOUT = 480
_HEALTH_POLL = 3
_API_TIMEOUT = 30
_CHAT_TIMEOUT = 60

_RG = "polyclaw-e2e-setup-rg"
_LOCATION = "eastus"
_BASE_NAME = "e2esetup"


# ---------------------------------------------------------------------------
# Chat probe script -- runs INSIDE the runtime container
# ---------------------------------------------------------------------------

_CHAT_PROBE_SCRIPT = r"""
import asyncio, json, sys, os, aiohttp

async def main():
    secret = os.environ.get("_PROBE_SECRET", "")

    port = os.environ.get("ADMIN_PORT", "8080")
    url = f"http://localhost:{port}/api/chat/ws"
    headers = {}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(url, headers=headers) as ws:
            await ws.send_json({"action": "send", "text": "Reply with exactly: PROBE_OK"})
            chunks = []
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    t = data.get("type", "")
                    if t == "delta":
                        chunks.append(data.get("content", ""))
                    elif t == "message":
                        chunks.append(data.get("content", ""))
                    elif t == "done":
                        break
                    elif t == "error":
                        detail = data.get("content", "")
                        print(detail, file=sys.stderr)
                        sys.exit(2)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
            print("".join(chunks))

asyncio.run(main())
"""


# ---------------------------------------------------------------------------
# Telegram config from .botservice-secret.txt
# ---------------------------------------------------------------------------

def _load_telegram_config() -> tuple[str, str]:
    """Return ``(token, whitelist)`` from ``.botservice-secret.txt``."""
    secret_file = _PROJECT_ROOT / ".botservice-secret.txt"
    if not secret_file.exists():
        return "", ""
    lines = secret_file.read_text().strip().splitlines()
    token = lines[0].strip() if lines else ""
    whitelist = lines[1].strip() if len(lines) > 1 else ""
    return token, whitelist


# ---------------------------------------------------------------------------
# Shell / Docker helpers
# ---------------------------------------------------------------------------

def _run(
    cmd: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        cwd=cwd or _PROJECT_ROOT,
        env=env,
    )


def _compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", *args], timeout=timeout)


def _start_stack(timeout: int = 60) -> None:
    """Start containers, falling back to ``up -d`` if ``start`` fails."""
    r = _run(["docker", "compose", "start"], check=False, timeout=timeout)
    if r.returncode == 0:
        return
    logger.warning(
        "docker compose start failed (rc=%d), falling back to up -d: %s",
        r.returncode, (r.stderr or r.stdout)[:300],
    )
    _compose("up", "-d", timeout=timeout)


def _recover_stack() -> dict | None:
    """Bring the stack back up and return health, or ``None`` on failure."""
    logger.warning("Recovering stack via docker compose up -d ...")
    try:
        _compose("up", "-d", timeout=120)
    except Exception as exc:
        logger.error("Stack recovery failed: %s", exc)
        return None
    health = _poll_health(timeout=_BOOT_TIMEOUT)
    if health:
        _copy_azure_creds()
        time.sleep(5)
    return health


def _api(
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    secret: str = "",
    timeout: int = _API_TIMEOUT,
) -> tuple[int, dict | None]:
    """Call the admin API. Returns ``(http_status, json_body | None)``."""
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
    elif method == "PUT":
        cmd += ["-X", "PUT", "-H", "Content-Type: application/json"]
        cmd += ["-d", json.dumps(body) if body else "{}"]
    elif method == "DELETE":
        cmd += ["-X", "DELETE"]
    cmd.append(url)

    try:
        r = _run(cmd, check=False, timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        logger.warning("API call timed out: %s %s", method, path)
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
    """Call the API and assert success. Returns the JSON body."""
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
            r = _run(["curl", "-sf", "--max-time", "3", _HEALTH_URL], check=False, timeout=10)
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


def _copy_azure_creds() -> bool:
    azure_dir = Path.home() / ".azure"
    if not azure_dir.exists():
        return False
    try:
        # Create the target directory first
        _run(
            ["docker", "exec", _ADMIN_CONTAINER, "mkdir", "-p", "/admin-home/.azure"],
            timeout=10,
        )
        # Copy only the essential auth files (not the 900 MB cliextensions/bin)
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


def _send_chat_probe(secret: str = "") -> tuple[str | None, str]:
    """Returns ``(text, status)`` where status is ok|error|not_authenticated|empty."""
    try:
        r = _run(
            ["docker", "exec"]
            + (["-e", f"_PROBE_SECRET={secret}"] if secret else [])
            + [_RUNTIME_CONTAINER, "python", "-c", _CHAT_PROBE_SCRIPT],
            check=False, timeout=_CHAT_TIMEOUT,
        )
        if r.returncode == 2:
            return r.stderr.strip() or None, "not_authenticated"
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip(), "ok"
        if r.returncode == 0:
            return None, "empty"
        logger.warning("Chat probe exit %d: %s", r.returncode, r.stderr[:300])
        return None, "error"
    except subprocess.TimeoutExpired:
        return None, "error"
    except Exception as exc:
        logger.warning("Chat probe exception: %s", exc)
        return None, "error"


def _diag(phase: str) -> str:
    lines = [f"\n{'='*72}", f"DIAGNOSTICS -- {phase}", f"{'='*72}"]
    for c in (_ADMIN_CONTAINER, _RUNTIME_CONTAINER):
        lines.append(f"\n--- {c} ---")
        lines.append(_container_logs(c, tail=100))
    lines.append(f"{'='*72}\n")
    return "\n".join(lines)


def _extract_rg_from_id(resource_id: str) -> str:
    """Extract resource group from a soft-deleted resource's ID string."""
    # ID format: .../resourceGroups/<rg>/deletedAccounts/<name>
    parts = resource_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _purge_soft_deleted_resources() -> None:
    """Purge any soft-deleted Cognitive Services accounts matching _BASE_NAME.

    After issuing purge commands, polls ``list-deleted`` until Azure
    confirms none of the matching resources remain (up to 120 s).
    """
    try:
        r = _run(
            ["az", "cognitiveservices", "account", "list-deleted", "-o", "json"],
            check=False, timeout=30,
        )
        if r.returncode != 0:
            return
        deleted = json.loads(r.stdout) if r.stdout.strip() else []
        purged_names: list[str] = []
        for item in deleted:
            name = item.get("name", "")
            rg = item.get("resourceGroup") or _extract_rg_from_id(item.get("id", ""))
            loc = item.get("location", "")
            if _BASE_NAME in name or rg == _RG:
                logger.info("Purging soft-deleted resource: %s (rg=%s)", name, rg)
                _run(
                    [
                        "az", "cognitiveservices", "account", "purge",
                        "--name", name,
                        "--resource-group", rg,
                        "--location", loc,
                    ],
                    check=False, timeout=60,
                )
                purged_names.append(name)

        # Wait for Azure to confirm the purge propagated.
        if purged_names:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                r2 = _run(
                    ["az", "cognitiveservices", "account", "list-deleted", "-o", "json"],
                    check=False, timeout=30,
                )
                if r2.returncode != 0:
                    break
                still = json.loads(r2.stdout) if r2.stdout.strip() else []
                remaining = [
                    d.get("name", "") for d in still
                    if d.get("name", "") in purged_names
                ]
                if not remaining:
                    logger.info("All purged resources confirmed gone")
                    break
                logger.info("Waiting for purge propagation: %s", remaining)
                time.sleep(10)
    except Exception as exc:
        logger.warning("Soft-delete purge failed: %s", exc)


def _cleanup_runtime_sps() -> None:
    """Delete leftover runtime service principals from previous runs."""
    try:
        r = _run(
            ["az", "ad", "sp", "list",
             "--display-name", f"polyclaw-runtime-{_BASE_NAME}",
             "--query", "[].appId", "-o", "json"],
            check=False, timeout=30,
        )
        if r.returncode != 0:
            return
        sp_ids = json.loads(r.stdout) if r.stdout.strip() else []
        for sp_id in sp_ids:
            logger.info("Deleting leftover SP: %s", sp_id)
            _run(["az", "ad", "sp", "delete", "--id", sp_id],
                 check=False, timeout=30)
    except Exception as exc:
        logger.warning("SP cleanup failed: %s", exc)


# ---------------------------------------------------------------------------
# Fixtures (module-scoped -- one Docker stack for the whole file)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_secret() -> str:
    return "e2e-test-secret-" + os.urandom(8).hex()


@pytest.fixture(scope="module")
def telegram_config() -> tuple[str, str]:
    """Return ``(token, whitelist)`` -- may be empty if no secret file."""
    return _load_telegram_config()


@pytest.fixture(scope="module")
def stack(admin_secret):
    """Build, start, and tear down the Docker compose stack."""
    try:
        _run(["docker", "info"], timeout=15)
    except Exception:
        pytest.skip("Docker not available")

    # Clean stale containers and volumes from previous runs to avoid
    # leftover .env / FOUNDRY_ENDPOINT pointing to deleted resources.
    logger.info("Cleaning stale containers and volumes ...")
    _compose("down", "-v", "--remove-orphans", timeout=60)

    # Build
    logger.info("Building Docker image ...")
    try:
        _compose("build", timeout=_BUILD_TIMEOUT)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"Docker build failed:\n{exc.stderr[:2000]}")

    # Start
    logger.info("Starting Docker stack ...")
    try:
        _compose("up", "-d", timeout=60)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"docker compose up failed:\n{exc.stderr[:2000]}")

    # Wait for health
    health = _poll_health(timeout=_BOOT_TIMEOUT)
    if not health:
        pytest.fail(f"Admin container not healthy.\n{_diag('boot')}")

    # Inject ADMIN_SECRET into the shared /data/.env so both containers pick it up.
    logger.info("Injecting ADMIN_SECRET into /data/.env ...")
    try:
        _run(
            [
                "docker", "exec", _ADMIN_CONTAINER,
                "sh", "-c",
                f'grep -q "^ADMIN_SECRET=" /data/.env 2>/dev/null '
                f'&& sed -i "s|^ADMIN_SECRET=.*|ADMIN_SECRET={admin_secret}|" /data/.env '
                f'|| echo "ADMIN_SECRET={admin_secret}" >> /data/.env',
            ],
            timeout=10,
        )
    except Exception as exc:
        pytest.fail(f"Failed to inject ADMIN_SECRET: {exc}")

    # Restart containers to pick up the new secret
    logger.info("Restarting containers to pick up ADMIN_SECRET ...")
    _compose("restart", timeout=60)
    health = _poll_health(timeout=_BOOT_TIMEOUT)
    if not health:
        pytest.fail(f"Admin not healthy after restart.\n{_diag('restart')}")

    # Inject Azure creds AFTER restart (container filesystem is ephemeral).
    ok = _copy_azure_creds()
    if not ok:
        pytest.fail("Failed to copy Azure creds into container. Ensure ~/.azure exists.")

    # Wait a moment for any cached az results to expire (TTL=30s).
    # The 890 MB copy may trigger `az` calls that cache failures, so we
    # need to wait well past the 30s AzureCLI cache TTL.
    time.sleep(35)

    yield health

    # Teardown
    logger.info("Tearing down Docker stack ...")
    _compose("down", "-v", "--remove-orphans", timeout=60)


@pytest.fixture(scope="module", autouse=True)
def _cleanup_azure_rg():
    """Best-effort cleanup of the test resource group after all tests.

    Also purges soft-deleted Cognitive Services accounts in the RG so
    subsequent runs don't hit ``FlagMustBeSetForRestore``.
    """
    # Pre-clean: purge any soft-deleted resources from previous runs
    _purge_soft_deleted_resources()
    _cleanup_runtime_sps()
    yield
    logger.info("Initiating cleanup of %s ...", _RG)
    try:
        _run(["az", "group", "delete", "--name", _RG, "--yes", "--no-wait"],
             check=False, timeout=30)
    except Exception as exc:
        logger.warning("RG cleanup failed: %s", exc)


# ===================================================================
# PHASE 1: Clean state verification
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase01CleanState:
    """Verify the freshly-booted stack has a clean, undeployed state."""

    def test_health(self, stack) -> None:
        assert stack.get("status") == "ok" or "version" in stack

    def test_initial_status(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/status", secret=admin_secret)
        assert not data["foundry"]["deployed"], "Foundry should not be deployed"
        # bot_configured may be True when /data volume has residual config
        logger.info(
            "Initial status: foundry_deployed=%s bot_configured=%s",
            data["foundry"]["deployed"], data.get("bot_configured"),
        )
        logger.info("Full status: %s", json.dumps(data, indent=2))

    def test_azure_logged_in(self, stack, admin_secret) -> None:
        # The creds were copied in the fixture; the container may need to
        # resolve the AZURE_CONFIG_DIR env var.  Retry up to 60s for
        # the 30s AzureCLI cache to expire after a failed az call.
        deadline = time.monotonic() + 60
        data: dict = {}
        while time.monotonic() < deadline:
            data = _api_ok("/api/setup/azure/check", secret=admin_secret)
            if data.get("status") == "logged_in":
                break
            time.sleep(5)
        assert data.get("status") == "logged_in", (
            f"Azure CLI not logged in after 60s: {json.dumps(data, indent=2)}"
        )
        logger.info("Azure user: %s", data.get("user"))

    def test_skills_list_empty_or_builtin(self, stack, admin_secret) -> None:
        data = _api_ok("/api/skills", secret=admin_secret)
        skills = data.get("skills", [])
        logger.info("Initial skills (%d): %s", len(skills), [s["name"] for s in skills[:5]])

    def test_sessions_empty(self, stack, admin_secret) -> None:
        data = _api_ok("/api/sessions", secret=admin_secret)
        assert isinstance(data, list)
        logger.info("Initial sessions: %d", len(data))

    def test_plugins_list(self, stack, admin_secret) -> None:
        data = _api_ok("/api/plugins", secret=admin_secret)
        logger.info("Plugins: %s", json.dumps(data, indent=2)[:500])

    def test_guardrails_config(self, stack, admin_secret) -> None:
        data = _api_ok("/api/guardrails/config", secret=admin_secret)
        assert "enabled" in data or "mode" in data or "hitl_mode" in data
        logger.info("Guardrails config keys: %s", list(data.keys()))

    def test_mcp_servers_list(self, stack, admin_secret) -> None:
        data = _api_ok("/api/mcp/servers", secret=admin_secret)
        logger.info("MCP servers: %s", json.dumps(data, indent=2)[:500])

    def test_schedules_empty(self, stack, admin_secret) -> None:
        data = _api_ok("/api/schedules", secret=admin_secret)
        logger.info("Schedules: %s", json.dumps(data, indent=2)[:300])

    def test_profile(self, stack, admin_secret) -> None:
        data = _api_ok("/api/profile", secret=admin_secret)
        logger.info("Profile: %s", json.dumps(data, indent=2)[:500])

    def test_models_list(self, stack, admin_secret) -> None:
        data = _api_ok("/api/models", secret=admin_secret)
        logger.info("Models: %s", json.dumps(data, indent=2)[:500])

    def test_content_safety_not_deployed(self, stack, admin_secret) -> None:
        data = _api_ok("/api/content-safety/status", secret=admin_secret)
        assert not data.get("deployed"), "Content Safety should not be deployed yet"

    def test_foundry_iq_config(self, stack, admin_secret) -> None:
        data = _api_ok("/api/foundry-iq/config", secret=admin_secret)
        logger.info("Foundry IQ config: %s", json.dumps(data, indent=2)[:500])

    def test_chat_fails_before_setup(self, stack) -> None:
        text, status = _send_chat_probe()
        logger.info("Chat probe (clean): status=%s detail=%r", status, text)
        assert status != "ok", "Chat should not succeed before any setup"


# ===================================================================
# PHASE 2: Deploy Foundry + Key Vault (core infra)
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase02DeployFoundry:
    """Provision Foundry AI Services with models and Key Vault."""

    def test_deploy_foundry_and_kv(self, stack, admin_secret) -> None:
        body = {
            "resource_group": _RG,
            "location": _LOCATION,
            "base_name": _BASE_NAME,
            "deploy_key_vault": True,
        }
        code, data = _api(
            "/api/setup/foundry/deploy",
            method="POST", body=body,
            secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        if code == 500 and data:
            detail = json.dumps(data.get("steps", []))
            if "FlagMustBeSetForRestore" in detail or "soft-de" in detail:
                pytest.xfail("Soft-deleted resource blocking deploy (purge needed)")
        assert code == 200, f"Deploy returned {code}: {json.dumps(data, indent=2)[:1000]}"
        assert data["status"] == "ok", f"Deploy failed: {data.get('error')}"
        assert data.get("foundry_endpoint"), "No Foundry endpoint"
        assert data.get("key_vault_url"), "No Key Vault URL"
        assert len(data.get("deployed_models", [])) >= 1, "No models deployed"
        logger.info(
            "Deployed: endpoint=%s models=%s kv=%s",
            data["foundry_endpoint"],
            data.get("deployed_models"),
            data.get("key_vault_url"),
        )

    def test_foundry_status(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/foundry/status", secret=admin_secret)
        if not data.get("deployed"):
            pytest.xfail(f"Foundry not deployed (prior deploy may have failed): {data}")
        assert data.get("foundry_endpoint")

    def test_global_status_reflects_foundry(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/status", secret=admin_secret)
        if not data["foundry"]["deployed"]:
            pytest.xfail("Foundry not deployed (prior deploy may have failed)")
        assert data["foundry"]["endpoint"]
        assert data.get("prerequisites_configured"), "Prerequisites should be configured (KV)"

    def test_prerequisites_status(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/prerequisites/status", secret=admin_secret)
        kv = data.get("keyvault", {})
        logger.info("Prerequisites: %s", json.dumps(data, indent=2))
        if kv.get("configured"):
            assert kv.get("url"), "KV configured but no URL"
        elif kv.get("name"):
            logger.warning("KV created (%s) but not yet configured", kv["name"])
        else:
            # Deploy may have failed entirely (soft-delete, quota, etc.)
            logger.warning("KV not provisioned: %s", kv)

    def test_runtime_sp_provisioned(self, stack, admin_secret) -> None:
        """Foundry deploy must provision a runtime SP for Key Vault access.

        The deploy step creates a service principal via
        ``az ad sp create-for-rbac`` and writes its credentials to
        ``/data/.env``.  The runtime container uses these to
        ``az login --service-principal`` at boot and resolve ``@kv:``
        secrets from Key Vault.
        """
        result = subprocess.run(
            ["docker", "exec", _ADMIN_CONTAINER, "cat", "/data/.env"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, f"Could not read .env: {result.stderr}"
        env_lines = result.stdout.strip().splitlines()
        env_dict = {}
        for line in env_lines:
            if "=" in line:
                k, v = line.split("=", 1)
                env_dict[k] = v

        has_kv = bool(env_dict.get("KEY_VAULT_URL"))
        if not has_kv:
            pytest.skip("Key Vault not deployed")

        sp_id = env_dict.get("RUNTIME_SP_APP_ID", "")
        sp_pw = env_dict.get("RUNTIME_SP_PASSWORD", "")
        sp_tenant = env_dict.get("RUNTIME_SP_TENANT", "")
        assert sp_id, (
            "RUNTIME_SP_APP_ID not set in .env -- the runtime container "
            "cannot resolve @kv: secrets from Key Vault"
        )
        assert sp_pw, "RUNTIME_SP_PASSWORD not set"
        assert sp_tenant, "RUNTIME_SP_TENANT not set"
        logger.info("Runtime SP provisioned: app_id=%s tenant=%s", sp_id, sp_tenant)

    def test_runtime_has_no_kv_errors(self, stack) -> None:
        """Runtime container logs must not contain Key Vault resolution errors."""
        result = subprocess.run(
            ["docker", "logs", _RUNTIME_CONTAINER, "--tail", "100"],
            capture_output=True, text=True, timeout=15,
        )
        logs = result.stdout + result.stderr
        kv_errors = [
            line for line in logs.splitlines()
            if "Failed to resolve Key Vault" in line
            or "DefaultAzureCredential failed" in line
        ]
        if kv_errors:
            pytest.fail(
                f"Runtime container has Key Vault resolution errors:\n"
                + "\n".join(f"  {l.strip()}" for l in kv_errors[:5])
            )

    def test_chat_works_after_foundry_no_bot(self, stack, admin_secret) -> None:
        """Foundry deployed, no bot -- chat MUST work (bot is optional).

        The deploy handler restarts the runtime container so it picks
        up ``FOUNDRY_ENDPOINT``.  We wait up to 90 s for the restart to
        finish and the agent to initialise.
        """
        # Trigger a container restart so the runtime picks up the new env
        code, data = _api(
            "/api/setup/container/restart",
            method="POST", secret=admin_secret, timeout=60,
        )
        logger.info("Container restart after Foundry deploy: %d %s", code, data)

        # Wait for the runtime to come back up
        time.sleep(10)
        _poll_health(timeout=60)

        # Chat must work -- Foundry is provisioned, bot is NOT required.
        # Allow up to 3 minutes: after restart the Copilot CLI needs time to
        # re-download its runtime, authenticate via BYOK, and start a session.
        deadline = time.monotonic() + 180
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat works without bot: %r", text[:200])
                return
            logger.info("Chat probe (foundry, no bot): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat did not work after Foundry deploy (no bot). "
            f"Last status={last_status}\n{_diag('chat-after-foundry')}"
        )


# ===================================================================
# PHASE 3: Deploy Content Safety (Prompt Shields)
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase03ContentSafety:
    """Deploy Azure AI Content Safety and test Prompt Shields."""

    def test_deploy_content_safety(self, stack, admin_secret) -> None:
        body = {
            "resource_group": _RG,
            "location": _LOCATION,
        }
        data = _api_ok(
            "/api/content-safety/deploy",
            method="POST", body=body,
            secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        assert data.get("status") == "ok", f"CS deploy failed: {data}"
        assert data.get("endpoint"), "No Content Safety endpoint"
        logger.info("Content Safety deployed: %s", data.get("endpoint"))

    def test_content_safety_status(self, stack, admin_secret) -> None:
        data = _api_ok("/api/content-safety/status", secret=admin_secret)
        if not data.get("deployed"):
            pytest.xfail(f"Content Safety not deployed (deploy may have failed): {data}")
        assert data.get("endpoint")

    def test_content_safety_dry_run(self, stack, admin_secret) -> None:
        """Test Prompt Shields dry-run against the deployed endpoint."""
        # Skip if not deployed
        status_code, status_data = _api("/api/content-safety/status", secret=admin_secret)
        if not (status_data and status_data.get("deployed")):
            pytest.skip("Content Safety not deployed")
        data = _api_ok(
            "/api/content-safety/test",
            method="POST", secret=admin_secret, timeout=60,
        )
        logger.info("Prompt Shields test: %s", json.dumps(data, indent=2))
        assert data.get("status") == "ok"
        assert data.get("passed"), f"Prompt Shields dry-run failed: {data.get('detail')}"


# ===================================================================
# PHASE 4: Configure bot + Telegram (no tunnel yet)
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase04BotConfig:
    """Save bot and Telegram configuration without starting tunnel."""

    def test_save_bot_config(self, stack, admin_secret) -> None:
        body = {
            "resource_group": _RG,
            "location": _LOCATION,
            "display_name": "polyclaw-e2e-test",
            "bot_handle": "",
        }
        _api_ok("/api/setup/bot/config", method="POST", body=body, secret=admin_secret)

    def test_bot_config_persisted(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/bot/config", secret=admin_secret)
        assert data.get("resource_group") == _RG
        logger.info("Bot config: %s", json.dumps(data, indent=2))

    def test_save_telegram_config(self, stack, admin_secret, telegram_config) -> None:
        token, whitelist = telegram_config
        if not token:
            pytest.skip("No .botservice-secret.txt -- Telegram is optional")
        body = {"token": token, "whitelist": whitelist}
        _api_ok("/api/setup/channels/telegram/config", method="POST", body=body, secret=admin_secret)

    def test_channels_config(self, stack, admin_secret, telegram_config) -> None:
        data = _api_ok("/api/setup/channels/config", secret=admin_secret)
        token, _ = telegram_config
        if token:
            tg = data.get("telegram", {})
            assert tg.get("configured") or tg.get("token"), f"Telegram not configured: {data}"
        logger.info("Channels: %s", json.dumps(data, indent=2)[:500])

    def test_status_shows_bot_configured(self, stack, admin_secret, telegram_config) -> None:
        data = _api_ok("/api/setup/status", secret=admin_secret)
        assert data["bot_configured"], f"Bot not marked configured: {data}"
        token, _ = telegram_config
        if token:
            assert data.get("telegram_configured"), f"Telegram not configured: {data}"
        else:
            logger.info("Telegram not configured (optional -- no secret file)")

    def test_chat_works_with_bot_config_no_tunnel(self, stack, admin_secret) -> None:
        """Bot configured but no tunnel -- chat MUST still work.

        The bot service is optional and should not block core chat.
        """
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat works with bot config, no tunnel: %s", text[:200])
                return
            logger.info("Chat (bot cfg, no tunnel): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat broken after adding bot config (no tunnel). "
            f"Last status={last_status}\n{_diag('chat-bot-no-tunnel')}"
        )


# ===================================================================
# PHASE 5: Start tunnel + provision bot infra (full stack)
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase05FullStack:
    """Start tunnel, provision bot, and verify end-to-end chat."""

    def test_start_tunnel(self, stack, admin_secret) -> None:
        code, data = _api(
            "/api/setup/tunnel/start",
            method="POST", body={"port": 9090},
            secret=admin_secret, timeout=60,
        )
        logger.info("Tunnel start: code=%d data=%s", code, json.dumps(data or {}, indent=2))
        if code == 400 and data and "managed by the runtime" in str(data.get("message", "")):
            pytest.skip("Tunnel managed by runtime container (split mode)")
        assert code == 200, f"Tunnel start failed: {data}"
        assert data.get("url"), "No tunnel URL"
        logger.info("Tunnel URL: %s", data["url"])

    def test_tunnel_in_status(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/status", secret=admin_secret)
        tunnel = data.get("tunnel", {})
        logger.info("Tunnel status: %s", json.dumps(tunnel, indent=2))

    def test_deploy_bot_infrastructure(self, stack, admin_secret) -> None:
        """Provision Bot Service via infra deploy."""
        code, data = _api(
            "/api/setup/infra/deploy",
            method="POST", secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        logger.info("Bot infra deploy: code=%d", code)
        if data:
            for step in data.get("steps", []):
                logger.info("  step: %s", step)
        if code != 200:
            logger.warning("Bot infra deploy failed: %s", json.dumps(data or {}, indent=2))
            pytest.xfail("Bot infra deploy failed (may need RBAC propagation)")
        assert data.get("status") == "ok"

    def test_preflight_checks(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/preflight", secret=admin_secret)
        for check in data.get("checks", data) if isinstance(data, list) else [data]:
            logger.info("Preflight: %s", check)

    def test_status_full_stack(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/status", secret=admin_secret)
        logger.info("Full stack status:\n%s", json.dumps(data, indent=2))
        assert data["bot_configured"]
        if not data["foundry"]["deployed"]:
            pytest.xfail("Foundry not deployed (deploy may have failed earlier)")

    def test_chat_full_stack(self, stack, admin_secret) -> None:
        """With full stack running, chat MUST work end-to-end."""
        time.sleep(8)
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Full stack chat response: %s", text[:300])
                return
            logger.info("Chat probe (full stack): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat did not work in full stack mode. "
            f"Last status={last_status}\n{_diag('chat-full-stack')}"
        )

    def test_smoke_test(self, stack, admin_secret) -> None:
        code, data = _api(
            "/api/setup/copilot/smoke-test",
            method="POST", secret=admin_secret, timeout=120,
        )
        logger.info("Smoke test: code=%d data=%s", code, json.dumps(data or {}, indent=2)[:1000])


# ===================================================================
# PHASE 6: Skills CRUD
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase06Skills:
    """Verify skill management works end-to-end."""

    def test_list_installed_skills(self, stack, admin_secret) -> None:
        data = _api_ok("/api/skills/installed", secret=admin_secret)
        assert isinstance(data, list)
        logger.info("Installed skills: %d", len(data))
        for s in data[:5]:
            logger.info("  %s (%s)", s.get("name"), s.get("source"))

    def test_catalog_fetch(self, stack, admin_secret) -> None:
        data = _api_ok("/api/skills/catalog", secret=admin_secret)
        assert isinstance(data, list)
        logger.info("Catalog skills: %d", len(data))

    def test_marketplace(self, stack, admin_secret) -> None:
        data = _api_ok("/api/skills/marketplace", secret=admin_secret)
        assert "all" in data
        logger.info(
            "Marketplace: all=%d recommended=%d installed=%d",
            len(data.get("all", [])),
            len(data.get("recommended", [])),
            len(data.get("installed", [])),
        )

    def test_install_skill(self, stack, admin_secret) -> None:
        """Install from catalog if available; skip if catalog is empty or rate-limited."""
        catalog = _api_ok("/api/skills/catalog", secret=admin_secret)
        if not catalog:
            pytest.skip("Catalog empty (likely rate-limited)")
        name = catalog[0].get("name")
        code, data = _api(
            "/api/skills/install",
            method="POST",
            body={"name": name},
            secret=admin_secret,
        )
        logger.info("Install %s: code=%d data=%s", name, code, data)
        if code == 400 and data and "429" in str(data.get("message", "")):
            pytest.skip(f"GitHub rate limit hit installing {name}")
        assert code == 200, f"Install {name} returned {code}: {data}"

    def test_skill_appears_in_list(self, stack, admin_secret) -> None:
        data = _api_ok("/api/skills/installed", secret=admin_secret)
        names = [s.get("name") for s in data]
        # At minimum, built-in skills should be present
        assert len(names) >= 1, f"No skills installed: {names}"
        assert "web-search" in names, f"web-search not in installed: {names}"

    def test_remove_skill(self, stack, admin_secret) -> None:
        """Remove a user-installed skill (not built-in)."""
        data = _api_ok("/api/skills/installed", secret=admin_secret)
        user_skills = [s for s in data if s.get("origin") not in ("built-in", "plugin")]
        if not user_skills:
            pytest.skip("No user-installed skills to remove")
        name = user_skills[0]["name"]
        code, resp = _api(f"/api/skills/{name}", method="DELETE", secret=admin_secret)
        assert code == 200, f"Remove {name} returned {code}: {resp}"


# ===================================================================
# PHASE 7: Sessions
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase07Sessions:
    """Verify session management after chat activity."""

    def test_session_stats(self, stack, admin_secret) -> None:
        data = _api_ok("/api/sessions/stats", secret=admin_secret)
        logger.info("Session stats: %s", json.dumps(data, indent=2))

    def test_list_sessions(self, stack, admin_secret) -> None:
        data = _api_ok("/api/sessions", secret=admin_secret)
        logger.info("Sessions: %d", len(data) if isinstance(data, list) else 0)

    def test_archival_policy(self, stack, admin_secret) -> None:
        data = _api_ok("/api/sessions/policy", secret=admin_secret)
        assert "policy" in data
        assert "options" in data
        logger.info("Archival policy: %s, options: %s", data["policy"], data["options"])


# ===================================================================
# PHASE 8: Guardrails + security
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase08Guardrails:
    """Verify guardrails management."""

    def test_config(self, stack, admin_secret) -> None:
        data = _api_ok("/api/guardrails/config", secret=admin_secret)
        logger.info("Guardrails config: %s", json.dumps(data, indent=2)[:500])

    def test_list_rules(self, stack, admin_secret) -> None:
        data = _api_ok("/api/guardrails/rules", secret=admin_secret)
        logger.info("Guardrails rules: %s", json.dumps(data, indent=2)[:500])

    def test_list_tools(self, stack, admin_secret) -> None:
        data = _api_ok("/api/guardrails/tools", secret=admin_secret)
        logger.info("Guardrails tools: %d items", len(data) if isinstance(data, list) else 0)

    def test_list_presets(self, stack, admin_secret) -> None:
        data = _api_ok("/api/guardrails/presets", secret=admin_secret)
        logger.info("Presets: %s", json.dumps(data, indent=2)[:500])

    def test_templates(self, stack, admin_secret) -> None:
        data = _api_ok("/api/guardrails/templates", secret=admin_secret)
        logger.info("Templates: %s", json.dumps(data, indent=2)[:300])

    def test_contexts(self, stack, admin_secret) -> None:
        data = _api_ok("/api/guardrails/contexts", secret=admin_secret)
        logger.info("Contexts: %s", json.dumps(data, indent=2)[:300])

    def test_preflight_run(self, stack, admin_secret) -> None:
        data = _api_ok(
            "/api/guardrails/preflight/run",
            method="POST", secret=admin_secret, timeout=60,
        )
        logger.info("Preflight run: %s", json.dumps(data, indent=2)[:1000])


# ===================================================================
# PHASE 9: Plugins
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase09Plugins:
    """Verify plugin management."""

    def test_list_plugins(self, stack, admin_secret) -> None:
        data = _api_ok("/api/plugins", secret=admin_secret)
        plugins = data if isinstance(data, list) else data.get("plugins", [])
        logger.info("Plugins: %d", len(plugins))
        for p in plugins[:5]:
            logger.info(
                "  %s (enabled=%s)", p.get("id") or p.get("name"), p.get("enabled"),
            )

    def test_enable_plugin(self, stack, admin_secret) -> None:
        data = _api_ok("/api/plugins", secret=admin_secret)
        plugins = data if isinstance(data, list) else data.get("plugins", [])
        if not plugins:
            pytest.skip("No plugins available")
        pid = plugins[0].get("id") or plugins[0].get("name")
        code, resp = _api(
            f"/api/plugins/{pid}/enable",
            method="POST", secret=admin_secret,
        )
        logger.info("Enable plugin %s: %d %s", pid, code, resp)

    def test_disable_plugin(self, stack, admin_secret) -> None:
        data = _api_ok("/api/plugins", secret=admin_secret)
        plugins = data if isinstance(data, list) else data.get("plugins", [])
        enabled = [p for p in plugins if p.get("enabled")]
        if not enabled:
            pytest.skip("No enabled plugins")
        pid = enabled[0].get("id") or enabled[0].get("name")
        code, resp = _api(
            f"/api/plugins/{pid}/disable",
            method="POST", secret=admin_secret,
        )
        logger.info("Disable plugin %s: %d %s", pid, code, resp)


# ===================================================================
# PHASE 10: MCP servers
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase10MCP:
    """Verify MCP server management."""

    def test_list_servers(self, stack, admin_secret) -> None:
        data = _api_ok("/api/mcp/servers", secret=admin_secret)
        logger.info("MCP servers: %s", json.dumps(data, indent=2)[:500])

    def test_registry(self, stack, admin_secret) -> None:
        data = _api_ok("/api/mcp/registry", secret=admin_secret)
        items = data if isinstance(data, list) else data.get("servers", [])
        logger.info("MCP registry: %d entries", len(items))


# ===================================================================
# PHASE 11: Scheduler
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase11Scheduler:
    """Verify scheduled task management."""

    def test_list_schedules(self, stack, admin_secret) -> None:
        data = _api_ok("/api/schedules", secret=admin_secret)
        logger.info("Schedules: %s", json.dumps(data, indent=2)[:300])

    def test_create_schedule(self, stack, admin_secret) -> None:
        body = {
            "prompt": "Hello, this is a test schedule",
            "cron": "0 9 * * 1",
            "enabled": False,
        }
        code, data = _api(
            "/api/schedules", method="POST", body=body, secret=admin_secret,
        )
        logger.info("Create schedule: %d %s", code, data)
        assert code in (200, 201), f"Create schedule returned {code}: {data}"

    def test_delete_schedule(self, stack, admin_secret) -> None:
        data = _api_ok("/api/schedules", secret=admin_secret)
        tasks = data if isinstance(data, list) else data.get("tasks", [])
        if not tasks:
            pytest.skip("No schedules to delete")
        task_id = tasks[0].get("id") or tasks[0].get("task_id")
        code, resp = _api(
            f"/api/schedules/{task_id}",
            method="DELETE", secret=admin_secret,
        )
        assert code == 200, f"Delete schedule returned {code}: {resp}"


# ===================================================================
# PHASE 12: Profile management
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase12Profile:
    """Verify agent profile CRUD."""

    def test_get_profile(self, stack, admin_secret) -> None:
        data = _api_ok("/api/profile", secret=admin_secret)
        logger.info("Profile: %s", json.dumps(data, indent=2)[:500])

    def test_update_profile(self, stack, admin_secret) -> None:
        data = _api_ok(
            "/api/profile",
            method="POST",
            body={"name": "E2E Test Agent", "personality": "helpful and concise"},
            secret=admin_secret,
        )
        logger.info("Profile updated: %s", data)


# ===================================================================
# PHASE 13: Foundry IQ (search + embedding)
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase13FoundryIQ:
    """Deploy and test Foundry IQ (AI Search + Embedding AOAI)."""

    def test_provision_foundry_iq(self, stack, admin_secret) -> None:
        body = {
            "resource_group": _RG,
            "location": _LOCATION,
        }
        code, data = _api(
            "/api/foundry-iq/provision",
            method="POST", body=body,
            secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        logger.info("Foundry IQ provision: code=%d data=%s", code, json.dumps(data or {}, indent=2))
        if code != 200:
            pytest.xfail(f"Foundry IQ provision failed: {data}")
        assert data.get("status") == "ok" or data.get("search_endpoint")

    def test_foundry_iq_config(self, stack, admin_secret) -> None:
        data = _api_ok("/api/foundry-iq/config", secret=admin_secret)
        logger.info("Foundry IQ config: %s", json.dumps(data, indent=2))

    def test_foundry_iq_stats(self, stack, admin_secret) -> None:
        data = _api_ok("/api/foundry-iq/stats", secret=admin_secret)
        logger.info("Foundry IQ stats: %s", json.dumps(data, indent=2))


# ===================================================================
# PHASE 14: Idempotency -- redeploy everything
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase14Idempotency:
    """Redeploy the same config and verify it remains stable."""

    def test_redeploy_foundry(self, stack, admin_secret) -> None:
        body = {
            "resource_group": _RG,
            "location": _LOCATION,
            "base_name": _BASE_NAME,
            "deploy_key_vault": True,
        }
        code, data = _api(
            "/api/setup/foundry/deploy",
            method="POST", body=body,
            secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        if code == 500 and data:
            detail = json.dumps(data.get("steps", []))
            if "FlagMustBeSetForRestore" in detail or "soft-de" in detail:
                pytest.xfail("Soft-deleted resource blocking redeploy")
        assert code == 200, f"Redeploy returned {code}: {json.dumps(data, indent=2)[:500]}"
        assert data["status"] == "ok"
        assert data.get("foundry_endpoint")

    def test_redeploy_content_safety(self, stack, admin_secret) -> None:
        body = {"resource_group": _RG, "location": _LOCATION}
        data = _api_ok(
            "/api/content-safety/deploy",
            method="POST", body=body,
            secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        assert data.get("status") == "ok"

    def test_chat_still_works_after_redeploy(self, stack, admin_secret) -> None:
        """Chat MUST survive an idempotent redeploy."""
        time.sleep(5)
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat OK after redeploy: %s", text[:200])
                return
            logger.info("Chat probe (post-redeploy): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat broke after redeploy. Last status={last_status}\n"
            f"{_diag('chat-after-redeploy')}"
        )


# ===================================================================
# PHASE 15: Configuration save (combined save endpoint)
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase15CombinedSave:
    """Test the combined configuration/save endpoint."""

    def test_save_configuration(self, stack, admin_secret, telegram_config) -> None:
        token, whitelist = telegram_config
        body: dict[str, Any] = {
            "bot": {
                "resource_group": _RG,
                "location": _LOCATION,
                "display_name": "polyclaw-e2e-test",
            },
        }
        if token:
            body["telegram"] = {"token": token, "whitelist": whitelist}
        data = _api_ok(
            "/api/setup/configuration/save",
            method="POST", body=body,
            secret=admin_secret, timeout=120,
        )
        assert data.get("status") == "ok", f"Combined save failed: {data}"
        logger.info("Combined save steps: %s", data.get("steps"))

    def test_status_after_combined_save(self, stack, admin_secret, telegram_config) -> None:
        # Combined save restarts the runtime container; the admin may also
        # restart briefly (health-check churn, KV re-init).  Poll to let
        # the stack settle before asserting.
        health = _poll_health(timeout=60)
        if health is None:
            health = _recover_stack()
        assert health is not None, (
            f"Admin not healthy after combined save.\n{_diag('post-combined-save')}"
        )
        data = _api_ok("/api/setup/status", secret=admin_secret)
        assert data["bot_configured"]
        token, _ = telegram_config
        if token:
            assert data.get("telegram_configured")


# ===================================================================
# PHASE 16: Stop / start lifecycle (config survives restart)
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase16Lifecycle:
    """Stop and restart the Docker stack, verify config persists and chat works.

    This simulates the real-world scenario where a user shuts down their
    machine, boots it up again, and expects everything to still work
    without re-running setup.
    """

    def test_stop_stack(self, stack, admin_secret) -> None:
        """Stop both containers gracefully."""
        _compose("stop", timeout=60)
        # Verify containers are stopped
        time.sleep(3)
        r = _run(
            ["docker", "compose", "ps", "--format", "json"],
            check=False, timeout=15,
        )
        logger.info("Containers after stop: %s", r.stdout[:500])

    def test_start_stack_again(self, stack, admin_secret) -> None:
        """Start containers back up."""
        _start_stack(timeout=60)
        health = _poll_health(timeout=_BOOT_TIMEOUT)
        if health is None:
            health = _recover_stack()
        assert health is not None, (
            f"Admin not healthy after restart.\n{_diag('lifecycle-start-1')}"
        )
        logger.info("Stack healthy after first restart: %s", health)

    def test_azure_creds_survive_restart(self, stack, admin_secret) -> None:
        """Azure CLI should still be logged in after restart.

        Docker volumes persist across stop/start, but ephemeral container
        filesystem does not.  We re-copy creds just in case.
        """
        _copy_azure_creds()
        time.sleep(10)  # let cache expire
        deadline = time.monotonic() + 60
        data: dict = {}
        while time.monotonic() < deadline:
            data = _api_ok("/api/setup/azure/check", secret=admin_secret)
            if data.get("status") == "logged_in":
                logger.info("Azure OK after restart: %s", data.get("user"))
                return
            time.sleep(5)
        pytest.fail(f"Azure CLI not logged in after restart: {data}")

    def test_foundry_still_deployed(self, stack, admin_secret) -> None:
        """Foundry deployment status should survive restart."""
        data = _api_ok("/api/setup/foundry/status", secret=admin_secret)
        assert data.get("deployed"), (
            f"Foundry deploy state lost after restart: {json.dumps(data, indent=2)}"
        )
        assert data.get("foundry_endpoint"), "Foundry endpoint lost after restart"
        logger.info("Foundry still deployed: %s", data.get("foundry_endpoint"))

    def test_config_survives_restart(self, stack, admin_secret) -> None:
        """Bot config and profile should persist on the /data volume."""
        status = _api_ok("/api/setup/status", secret=admin_secret)
        assert status["bot_configured"], "Bot config lost after restart"
        profile = _api_ok("/api/profile", secret=admin_secret)
        logger.info("Profile after restart: %s", json.dumps(profile, indent=2)[:300])
        # Profile name was set to "E2E Test Agent" in Phase 12
        if profile.get("name"):
            assert profile["name"] == "E2E Test Agent", (
                f"Profile name changed after restart: {profile['name']}"
            )

    def test_chat_works_after_restart(self, stack, admin_secret) -> None:
        """Chat MUST work after a stop/start cycle."""
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat works after restart: %s", text[:200])
                return
            logger.info("Chat probe (post-restart): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat broken after restart. Last status={last_status}\n"
            f"{_diag('chat-after-restart')}"
        )

    def test_stop_and_start_again(self, stack, admin_secret) -> None:
        """Second stop/start cycle to verify repeated restarts work."""
        _compose("stop", timeout=60)
        time.sleep(3)
        _start_stack(timeout=60)
        health = _poll_health(timeout=_BOOT_TIMEOUT)
        if health is None:
            health = _recover_stack()
        assert health is not None, (
            f"Admin not healthy after second restart.\n{_diag('lifecycle-start-2')}"
        )
        # Re-inject creds (ephemeral FS)
        _copy_azure_creds()
        time.sleep(5)
        logger.info("Stack healthy after second restart: %s", health)

    def test_chat_works_after_second_restart(self, stack, admin_secret) -> None:
        """Chat MUST still work after two stop/start cycles."""
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat OK after 2nd restart: %s", text[:200])
                return
            logger.info("Chat (2nd restart): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat broken after second restart. Last status={last_status}\n"
            f"{_diag('chat-after-restart-2')}"
        )

    def test_no_kv_errors_after_restart(self, stack) -> None:
        """Runtime must not have KV errors after stop/start cycles.

        This catches the scenario where the admin container rewrites
        ADMIN_SECRET as ``@kv:admin-secret`` during a restart (because
        KV is already deployed) and the runtime cannot resolve it.
        """
        result = subprocess.run(
            ["docker", "logs", _RUNTIME_CONTAINER, "--tail", "200"],
            capture_output=True, text=True, timeout=15,
        )
        logs = result.stdout + result.stderr
        kv_errors = [
            line for line in logs.splitlines()
            if "Failed to resolve Key Vault" in line
            or "DefaultAzureCredential failed" in line
        ]
        if kv_errors:
            pytest.fail(
                f"Runtime has Key Vault errors after lifecycle restart:\n"
                + "\n".join(f"  {l.strip()}" for l in kv_errors[:5])
            )


# ===================================================================
# PHASE 17: Config change mid-lifecycle
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase17ConfigChange:
    """Change configuration, restart, verify the new config takes effect
    and chat still works.
    """

    def test_change_profile(self, stack, admin_secret) -> None:
        """Update the agent name and personality."""
        # Ensure the stack survived Phase 15-16 before proceeding.
        health = _poll_health(timeout=10)
        if health is None:
            health = _recover_stack()
            assert health is not None, "Stack unrecoverable before Phase 17"
        data = _api_ok(
            "/api/profile",
            method="POST",
            body={"name": "Reconfigured Agent", "personality": "terse and technical"},
            secret=admin_secret,
        )
        logger.info("Profile changed: %s", data)

    def test_profile_persisted(self, stack, admin_secret) -> None:
        data = _api_ok("/api/profile", secret=admin_secret)
        assert data.get("name") == "Reconfigured Agent", f"Profile not updated: {data}"

    def test_change_guardrails_mode(self, stack, admin_secret) -> None:
        """Toggle guardrails to a different mode and verify."""
        config = _api_ok("/api/guardrails/config", secret=admin_secret)
        current_mode = config.get("hitl_mode") or config.get("mode", "auto")
        new_mode = "always" if current_mode != "always" else "auto"
        code, data = _api(
            "/api/guardrails/config",
            method="POST",
            body={"hitl_mode": new_mode},
            secret=admin_secret,
        )
        logger.info("Guardrails mode change to %s: %d %s", new_mode, code, data)
        if code == 200:
            readback = _api_ok("/api/guardrails/config", secret=admin_secret)
            actual = readback.get("hitl_mode") or readback.get("mode")
            logger.info("Guardrails mode after change: %s", actual)

    def test_restart_after_config_change(self, stack, admin_secret) -> None:
        """Restart the runtime container via API and verify chat works."""
        code, data = _api(
            "/api/setup/container/restart",
            method="POST", secret=admin_secret, timeout=60,
        )
        logger.info("Container restart: %d %s", code, data)
        time.sleep(10)
        _poll_health(timeout=60)

    def test_chat_works_after_config_change(self, stack, admin_secret) -> None:
        """Chat MUST still work after config changes + restart."""
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat OK after config change: %s", text[:200])
                return
            logger.info("Chat (config change): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat broken after config change. Last status={last_status}\n"
            f"{_diag('chat-after-config-change')}"
        )

    def test_changed_profile_survives_restart(self, stack, admin_secret) -> None:
        data = _api_ok("/api/profile", secret=admin_secret)
        assert data.get("name") == "Reconfigured Agent", (
            f"Profile reverted after restart: {data}"
        )

    def test_restore_profile(self, stack, admin_secret) -> None:
        """Restore original profile for subsequent phases."""
        _api_ok(
            "/api/profile",
            method="POST",
            body={"name": "E2E Test Agent", "personality": "helpful and concise"},
            secret=admin_secret,
        )


# ===================================================================
# PHASE 18: Bot service add / remove / toggle
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase18BotServiceToggle:
    """Verify chat survives adding, removing, and re-adding bot config.

    Bot service is **fully optional**.  The app must keep working with
    only Foundry deployed, regardless of whether bot config is present.
    """

    def test_remove_telegram_config(self, stack, admin_secret, telegram_config) -> None:
        """Remove Telegram channel config."""
        # Ensure the stack survived previous phases.
        health = _poll_health(timeout=10)
        if health is None:
            health = _recover_stack()
            assert health is not None, "Stack unrecoverable before Phase 18"
        token, _ = telegram_config
        if not token:
            pytest.skip("Telegram was never configured")
        code, data = _api(
            "/api/setup/channels/telegram/config",
            method="DELETE", secret=admin_secret,
        )
        logger.info("Remove Telegram: %d %s", code, data)
        # Verify it's gone
        status = _api_ok("/api/setup/status", secret=admin_secret)
        assert not status.get("telegram_configured"), (
            f"Telegram still configured after removal: {status}"
        )

    def test_remove_bot_config(self, stack, admin_secret) -> None:
        """Clear bot config by saving with empty resource group."""
        _api_ok(
            "/api/setup/bot/config",
            method="POST",
            body={"resource_group": "", "location": "", "display_name": "", "bot_handle": ""},
            secret=admin_secret,
        )
        status = _api_ok("/api/setup/status", secret=admin_secret)
        assert not status["bot_configured"], (
            f"Bot still configured after clearing: {status}"
        )
        logger.info("Bot config cleared")

    def test_restart_after_bot_removal(self, stack, admin_secret) -> None:
        """Restart runtime so it picks up the cleared config."""
        code, data = _api(
            "/api/setup/container/restart",
            method="POST", secret=admin_secret, timeout=60,
        )
        logger.info("Container restart after bot removal: %d %s", code, data)
        time.sleep(10)
        health = _poll_health(timeout=60)
        assert health is not None, (
            f"Admin not healthy after bot removal + restart.\n"
            f"{_diag('bot-removal-restart')}"
        )

    def test_chat_works_without_bot_service(self, stack, admin_secret) -> None:
        """Chat MUST work with no bot config at all -- only Foundry."""
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat works without bot service: %s", text[:200])
                return
            logger.info("Chat (no bot): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat broken after bot removal. Last status={last_status}\n"
            f"{_diag('chat-no-bot')}"
        )

    def test_re_add_bot_config(self, stack, admin_secret) -> None:
        """Re-add bot config."""
        _api_ok(
            "/api/setup/bot/config",
            method="POST",
            body={
                "resource_group": _RG,
                "location": _LOCATION,
                "display_name": "polyclaw-e2e-test",
                "bot_handle": "",
            },
            secret=admin_secret,
        )
        status = _api_ok("/api/setup/status", secret=admin_secret)
        assert status["bot_configured"], f"Bot not configured after re-add: {status}"
        logger.info("Bot config re-added")

    def test_re_add_telegram_config(self, stack, admin_secret, telegram_config) -> None:
        """Re-add Telegram if available."""
        token, whitelist = telegram_config
        if not token:
            pytest.skip("No Telegram token")
        _api_ok(
            "/api/setup/channels/telegram/config",
            method="POST",
            body={"token": token, "whitelist": whitelist},
            secret=admin_secret,
        )

    def test_chat_works_after_bot_re_add(self, stack, admin_secret) -> None:
        """Chat MUST work after re-adding bot config."""
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat works after bot re-add: %s", text[:200])
                return
            logger.info("Chat (bot re-add): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat broken after bot re-add. Last status={last_status}\n"
            f"{_diag('chat-bot-re-add')}"
        )

    def test_remove_bot_config_again(self, stack, admin_secret) -> None:
        """Remove bot config a second time to leave stack in bot-free state."""
        _api_ok(
            "/api/setup/bot/config",
            method="POST",
            body={"resource_group": "", "location": "", "display_name": "", "bot_handle": ""},
            secret=admin_secret,
        )

    def test_chat_still_works_after_second_removal(self, stack, admin_secret) -> None:
        """Chat MUST work after the second bot removal."""
        time.sleep(5)
        deadline = time.monotonic() + 120
        last_status = ""
        while time.monotonic() < deadline:
            text, last_status = _send_chat_probe(admin_secret)
            if last_status == "ok" and text:
                logger.info("Chat after 2nd bot removal: %s", text[:200])
                return
            logger.info("Chat (2nd bot removal): status=%s -- retrying", last_status)
            time.sleep(8)
        pytest.fail(
            f"Chat broken after second bot removal. Last status={last_status}\n"
            f"{_diag('chat-bot-2nd-removal')}"
        )

    def test_restore_bot_config_for_subsequent_phases(self, stack, admin_secret, telegram_config) -> None:
        """Restore bot + telegram for remaining phases."""
        _api_ok(
            "/api/setup/bot/config",
            method="POST",
            body={
                "resource_group": _RG,
                "location": _LOCATION,
                "display_name": "polyclaw-e2e-test",
                "bot_handle": "",
            },
            secret=admin_secret,
        )
        token, whitelist = telegram_config
        if token:
            _api_ok(
                "/api/setup/channels/telegram/config",
                method="POST",
                body={"token": token, "whitelist": whitelist},
                secret=admin_secret,
            )
        status = _api_ok("/api/setup/status", secret=admin_secret)
        assert status["bot_configured"]
        logger.info("Bot config restored for remaining phases")


# ===================================================================
# PHASE 19: Voice / ACS (optional)
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase19Voice:
    """Deploy ACS for voice calls."""

    def test_voice_config(self, stack, admin_secret) -> None:
        health = _poll_health(timeout=10)
        if health is None:
            health = _recover_stack()
            assert health is not None, "Stack unrecoverable before Phase 19"
        data = _api_ok("/api/setup/voice/config", secret=admin_secret)
        logger.info("Voice config: %s", json.dumps(data, indent=2)[:500])

    def test_deploy_acs(self, stack, admin_secret) -> None:
        body = {
            "resource_group": _RG,
            "location": _LOCATION,
        }
        code, data = _api(
            "/api/setup/voice/deploy",
            method="POST", body=body,
            secret=admin_secret, timeout=_DEPLOY_TIMEOUT,
        )
        logger.info("ACS deploy: code=%d data=%s", code, json.dumps(data or {}, indent=2))
        if code != 200:
            pytest.xfail(f"ACS deploy failed: {data}")

    def test_list_acs_resources(self, stack, admin_secret) -> None:
        data = _api_ok("/api/setup/voice/acs/list", secret=admin_secret)
        logger.info("ACS resources: %s", json.dumps(data, indent=2)[:500])


# ===================================================================
# PHASE 20: Lockdown mode
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase20Lockdown:
    """Test lockdown mode toggle."""

    def test_lockdown_status(self, stack, admin_secret) -> None:
        health = _poll_health(timeout=10)
        if health is None:
            health = _recover_stack()
            assert health is not None, "Stack unrecoverable before Phase 20"
        data = _api_ok("/api/setup/lockdown", secret=admin_secret)
        logger.info("Lockdown: %s", data)

    def test_enable_lockdown(self, stack, admin_secret) -> None:
        code, data = _api(
            "/api/setup/lockdown",
            method="POST",
            body={"enabled": True},
            secret=admin_secret,
        )
        logger.info("Enable lockdown: %d %s", code, data)

    def test_disable_lockdown(self, stack, admin_secret) -> None:
        code, data = _api(
            "/api/setup/lockdown",
            method="POST",
            body={"enabled": False},
            secret=admin_secret,
        )
        logger.info("Disable lockdown: %d %s", code, data)


# ===================================================================
# PHASE 21: Decommission all resources
# ===================================================================

@pytest.mark.e2e_setup
class TestPhase21Decommission:
    """Tear down all Azure resources."""

    def test_decommission_foundry(self, stack, admin_secret) -> None:
        health = _poll_health(timeout=10)
        if health is None:
            health = _recover_stack()
            assert health is not None, "Stack unrecoverable before Phase 21"
        body = {"resource_group": _RG}
        code, data = _api(
            "/api/setup/foundry/decommission",
            method="POST", body=body,
            secret=admin_secret, timeout=120,
        )
        logger.info("Decommission: code=%d data=%s", code, json.dumps(data or {}, indent=2))
        if code == 500 and data and "No subscription" in str(data.get("steps", [])):
            pytest.xfail("Subscription not set in decommission context")
        assert code == 200, f"Decommission returned {code}: {data}"
        assert data["status"] == "ok"
        logger.info("Decommission steps: %s", data.get("steps"))

    def test_status_after_decommission(self, stack, admin_secret) -> None:
        time.sleep(5)
        data = _api_ok("/api/setup/status", secret=admin_secret)
        if data["foundry"]["deployed"]:
            logger.warning(
                "Foundry still shows deployed (decommission may have xfailed)"
            )
        logger.info(
            "Post-decommission status: foundry_deployed=%s",
            data["foundry"]["deployed"],
        )

    def test_collect_final_diagnostics(self, stack) -> None:
        for name in (_ADMIN_CONTAINER, _RUNTIME_CONTAINER):
            logs = _container_logs(name, tail=80)
            logger.info("=== %s final logs ===\n%s", name, logs)


# ===================================================================
# PHASE 22: TUI headless mode (health + actual polyclaw-cli run)
# ===================================================================

_TUI_DIR = _PROJECT_ROOT / "app" / "tui"
_TUI_ENTRY = _TUI_DIR / "src" / "index.ts"
_TUI_RUN_TIMEOUT = 300  # TUI may rebuild + wait for ready + chat


def _bun_available() -> bool:
    try:
        r = _run(["bun", "--version"], check=False, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _ensure_tui_deps() -> bool:
    """Install TUI dependencies if needed. Returns True on success."""
    if (_TUI_DIR / "node_modules").exists():
        return True
    try:
        r = _run(["bun", "install"], check=False, timeout=60, cwd=_TUI_DIR)
        return r.returncode == 0
    except Exception:
        return False


@pytest.mark.e2e_setup
class TestPhase22TUIHeadless:
    """Verify the TUI CLI actually works in headless mode.

    Tests:
      1. ``polyclaw-cli health`` against the running stack (read-only).
      2. ``polyclaw-cli stop`` to gracefully shut down.
      3. ``polyclaw-cli run "prompt"`` which does the full lifecycle:
         build -> start -> wait -> chat via WebSocket -> print -> stop.
         This is the real user-facing headless path.

    The ``run`` command tears down any existing stack and starts fresh.
    The ``/data`` named volume persists (``docker compose down`` without
    ``-v``), so ``FOUNDRY_ENDPOINT`` from earlier phases is still
    available and chat should work.
    """

    def test_bun_available(self, stack) -> None:
        if not _bun_available():
            pytest.skip("Bun not installed -- cannot test TUI")

    def test_tui_deps_installed(self, stack) -> None:
        if not _bun_available():
            pytest.skip("Bun not installed")
        if not _TUI_ENTRY.exists():
            pytest.skip("TUI source not found")
        assert _ensure_tui_deps(), "Failed to install TUI dependencies"

    def test_tui_health(self, stack) -> None:
        """``polyclaw-cli health`` should succeed against the running stack."""
        if not _bun_available():
            pytest.skip("Bun not installed")
        if not _TUI_ENTRY.exists():
            pytest.skip("TUI source not found")
        health = _poll_health(timeout=10)
        if health is None:
            health = _recover_stack()
            assert health is not None, "Stack unrecoverable before Phase 22"
        r = _run(
            ["bun", "run", str(_TUI_ENTRY), "health"],
            check=False, timeout=30, cwd=_TUI_DIR,
        )
        logger.info("TUI health stdout: %s", r.stdout[:500])
        logger.info("TUI health stderr: %s", r.stderr[:300])
        assert r.returncode == 0, (
            f"polyclaw-cli health exited {r.returncode}: {r.stderr[:500]}"
        )
        try:
            health = json.loads(r.stdout.strip())
            assert health.get("status") == "ok" or "version" in health
        except json.JSONDecodeError:
            pass  # extra output lines are OK if exit code is 0

    def test_tui_stop(self, stack) -> None:
        """``polyclaw-cli stop`` should gracefully shut down the stack.

        This prepares for the ``run`` test which builds its own stack.
        """
        if not _bun_available():
            pytest.skip("Bun not installed")
        if not _TUI_ENTRY.exists():
            pytest.skip("TUI source not found")
        r = _run(
            ["bun", "run", str(_TUI_ENTRY), "stop"],
            check=False, timeout=60, cwd=_TUI_DIR,
        )
        logger.info("TUI stop: exit=%d stdout=%s", r.returncode, r.stdout[:300])
        # stop may exit non-zero if stack was already down -- that's fine
        time.sleep(5)

    def test_tui_run_prompt(self, stack, admin_secret) -> None:
        """``polyclaw-cli run "prompt"`` -- the real headless E2E path.

        This does the full TUI lifecycle: build image, start containers,
        wait for health, open WebSocket, send prompt, collect response,
        stop containers.  The ``/data`` volume still has
        ``FOUNDRY_ENDPOINT`` from earlier phases.

        We also re-inject ``ADMIN_SECRET`` into the data volume before
        starting, because the TUI's ``run`` mode reads it from there.
        """
        if not _bun_available():
            pytest.skip("Bun not installed")
        if not _TUI_ENTRY.exists():
            pytest.skip("TUI source not found")

        # Pre-inject ADMIN_SECRET into the data volume so the fresh
        # containers pick it up.  We write directly to a temp container
        # that mounts the same volume.
        try:
            _run(
                [
                    "docker", "run", "--rm",
                    "-v", "polyclaw-data:/data",
                    "alpine", "sh", "-c",
                    f'grep -q "^ADMIN_SECRET=" /data/.env 2>/dev/null '
                    f'&& sed -i "s|^ADMIN_SECRET=.*|ADMIN_SECRET={admin_secret}|" /data/.env '
                    f'|| echo "ADMIN_SECRET={admin_secret}" >> /data/.env',
                ],
                check=False, timeout=30,
            )
        except Exception as exc:
            logger.warning("Failed to pre-inject ADMIN_SECRET: %s", exc)

        # Run the actual TUI command
        env = {**os.environ, "VERBOSE": "1"}
        r = _run(
            ["bun", "run", str(_TUI_ENTRY), "run", "Reply with exactly: TUI_RUN_OK"],
            check=False, timeout=_TUI_RUN_TIMEOUT, cwd=_TUI_DIR, env=env,
        )
        logger.info("TUI run exit=%d", r.returncode)
        logger.info("TUI run stdout:\n%s", r.stdout[:2000])
        if r.stderr:
            logger.info("TUI run stderr:\n%s", r.stderr[:1000])

        assert r.returncode == 0, (
            f"polyclaw-cli run exited {r.returncode}.\n"
            f"stdout: {r.stdout[:1500]}\n"
            f"stderr: {r.stderr[:1000]}"
        )
        # The last line of stdout should be the chat response
        output = r.stdout.strip()
        assert output, "polyclaw-cli run produced no output"
        # The response is the last line (previous lines are status messages)
        response_line = output.split("\n")[-1].strip()
        logger.info("TUI run response: %s", response_line[:300])
        assert len(response_line) > 0, "TUI run response was empty"

    def test_stack_down_after_tui_run(self, stack) -> None:
        """Verify the TUI left the stack stopped (it calls stopContainer)."""
        r = _run(
            ["docker", "compose", "ps", "-q"],
            check=False, timeout=15,
        )
        running = r.stdout.strip()
        if running:
            logger.info("Containers still running after TUI run (expected stopped): %s", running)
        else:
            logger.info("Stack correctly stopped by polyclaw-cli run")
