"""Copilot SDK smoke test runner."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..agent.agent import Agent
from ..config.settings import cfg
from ..services.cloud.github import GitHubAuth

logger = logging.getLogger(__name__)


class SmokeTestRunner:
    """Runs progressive Copilot SDK smoke checks."""

    def __init__(self, gh: GitHubAuth) -> None:
        self._gh = gh
        self._steps: list[dict[str, Any]] = []

    async def run(self) -> dict[str, Any]:
        if not self._check_auth():
            return self._fail("Not authenticated.")
        if not self._check_cli():
            return self._fail("copilot CLI not found.")
        self._check_version()

        agent = Agent()
        try:
            if not await self._start_sdk(agent):
                return self._fail("SDK start failed.")
            if not await self._create_session(agent):
                return self._fail("Session creation failed.")
            if not await self._test_prompt(agent):
                return self._fail("Test prompt failed.")
        finally:
            await agent.stop()

        self._check_keyvault()
        await self._check_mcp_servers()
        self._check_state_files()

        has_failure = any(not s["ok"] for s in self._steps)
        return {
            "status": "ok" if not has_failure else "warning",
            "steps": self._steps,
            "message": (
                "All smoke checks passed."
                if not has_failure
                else "SDK works but some peripheral checks failed."
            ),
        }

    def _step(self, name: str, ok: bool, detail: str = "") -> None:
        self._steps.append({"step": name, "ok": ok, "detail": detail})

    def _fail(self, message: str) -> dict[str, Any]:
        return {"status": "error", "steps": self._steps, "message": message}

    def _check_auth(self) -> bool:
        st = self._gh.status()
        self._step("gh_auth", st.get("authenticated", False), st.get("details", ""))
        return st.get("authenticated", False) or bool(cfg.github_token)

    def _check_cli(self) -> bool:
        path = shutil.which("copilot")
        self._step("copilot_cli", bool(path), path or "not found")
        return bool(path)

    def _check_version(self) -> None:
        try:
            r = subprocess.run(
                ["copilot", "--version"],
                capture_output=True, text=True, timeout=10,
            )
            self._step(
                "copilot_version", r.returncode == 0, (r.stdout + r.stderr).strip()
            )
        except Exception as exc:
            self._step("copilot_version", False, str(exc))

    async def _start_sdk(self, agent: Agent) -> bool:
        try:
            await asyncio.wait_for(agent.start(), timeout=30)
            self._step("sdk_start", True, "CopilotClient started")
            return True
        except Exception as exc:
            self._step("sdk_start", False, str(exc))
            return False

    async def _create_session(self, agent: Agent) -> bool:
        try:
            await asyncio.wait_for(agent.new_session(), timeout=30)
            self._step("create_session", True, f"model={cfg.copilot_model}")
            return True
        except Exception as exc:
            self._step("create_session", False, str(exc))
            return False

    async def _test_prompt(self, agent: Agent) -> bool:
        try:
            reply = await asyncio.wait_for(
                agent.send("Reply with exactly: SMOKE_TEST_OK"), timeout=60
            )
            ok = bool(reply and "SMOKE_TEST_OK" in reply)
            self._step("send_prompt", ok, reply or "(no reply)")
            return ok
        except Exception as exc:
            self._step("send_prompt", False, str(exc))
            return False

    def _check_keyvault(self) -> None:
        from ..services.keyvault import kv

        if not kv.enabled:
            self._step("keyvault", True, "Not configured (plaintext mode)")
            return
        try:
            secrets = kv.list_secrets()
            self._step(
                "keyvault", True,
                f"Connected to {kv.url} ({len(secrets)} secret(s))",
            )
        except Exception as exc:
            self._step("keyvault", False, f"Unreachable: {exc}")

    async def _check_mcp_servers(self) -> None:
        from ..state.mcp_config import McpConfigStore

        store = McpConfigStore()
        servers = store.list_servers()
        enabled = [s for s in servers if s.get("enabled", False)]

        if not enabled:
            self._step("mcp_servers", True, "No MCP servers enabled")
            return

        for server in enabled:
            name = server.get("name", "unknown")
            stype = server.get("type", "")
            step_name = f"mcp_{name}"

            if stype in ("http", "sse"):
                await self._probe_remote_mcp(step_name, server)
            elif stype in ("local", "stdio"):
                self._probe_local_mcp(step_name, server)
            else:
                self._step(step_name, False, f"Unknown type '{stype}'")

    async def _probe_remote_mcp(
        self, step_name: str, server: dict[str, Any]
    ) -> None:
        import aiohttp

        url = server.get("url", "")
        if not url:
            self._step(step_name, False, "No URL configured")
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    ok = resp.status < 500
                    self._step(step_name, ok, f"{url} -> HTTP {resp.status}")
        except Exception as exc:
            self._step(step_name, False, f"{url} -> {exc}")

    def _probe_local_mcp(self, step_name: str, server: dict[str, Any]) -> None:
        command = server.get("command", "")
        if not command:
            self._step(step_name, False, "No command configured")
            return
        path = shutil.which(command)
        self._step(step_name, bool(path), f"{command} -> {path or 'not found'}")

    def _check_state_files(self) -> None:
        validators: dict[str, _StateFileValidator] = {
            "mcp_servers.json": _StateFileValidator(
                required_keys=["servers"], type_checks={"servers": dict}
            ),
            "infra.json": _StateFileValidator(
                required_keys=["bot", "channels"],
                type_checks={"bot": dict, "channels": dict},
            ),
            "agent_profile.json": _StateFileValidator(required_keys=["name"]),
            "scheduler.json": _StateFileValidator(),
        }

        data_dir = cfg.data_dir
        if not data_dir.is_dir():
            self._step("state_files", True, "Data directory does not exist yet")
            return

        checked = 0
        problems: list[str] = []

        for filename, validator in validators.items():
            path = data_dir / filename
            if not path.exists():
                continue
            checked += 1
            error = validator.check(path)
            if error:
                problems.append(f"{filename}: {error}")

        for path in data_dir.glob("*.json"):
            if path.name in validators:
                continue
            checked += 1
            error = _StateFileValidator().check(path)
            if error:
                problems.append(f"{path.name}: {error}")

        sessions_dir = data_dir / "sessions"
        if sessions_dir.is_dir():
            for path in sessions_dir.glob("*.json"):
                checked += 1
                error = _StateFileValidator(
                    required_keys=["id", "messages"],
                    type_checks={"messages": list},
                ).check(path)
                if error:
                    problems.append(f"sessions/{path.name}: {error}")

        if problems:
            self._step("state_files", False, "; ".join(problems))
        else:
            self._step(
                "state_files", True,
                f"Validated {checked} state file(s)" if checked else "No state files yet",
            )


class _StateFileValidator:
    """Lightweight JSON-file validator."""

    def __init__(
        self,
        required_keys: list[str] | None = None,
        type_checks: dict[str, type] | None = None,
    ) -> None:
        self._required_keys = required_keys or []
        self._type_checks = type_checks or {}

    def check(self, path: Path) -> str | None:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            return f"invalid JSON: {exc}"
        except OSError as exc:
            return f"read error: {exc}"

        if not isinstance(data, dict | list):
            return f"expected dict or list, got {type(data).__name__}"

        if isinstance(data, dict):
            for key in self._required_keys:
                if key not in data:
                    return f"missing key '{key}'"
            for key, expected in self._type_checks.items():
                if key in data and not isinstance(data[key], expected):
                    return (
                        f"key '{key}' should be {expected.__name__}, "
                        f"got {type(data[key]).__name__}"
                    )
        return None
