"""Agent sandbox executor -- runs agent commands in ACA Dynamic Sessions.

.. warning:: This feature is experimental and may change or be removed in
   future releases.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shlex
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import aiohttp

from .config.settings import cfg
from .state.sandbox_config import SandboxConfigStore

logger = logging.getLogger(__name__)

API_VERSION = "2024-02-02-preview"
TOKEN_SCOPE = "https://dynamicsessions.io/.default"
MAX_ZIP_SIZE = 100 * 1024 * 1024

_SHELL_TOOL_PATTERNS = ("terminal", "shell", "bash", "command")
_SESSION_IDLE_TIMEOUT = 60
_UPLOAD_MAX_RETRIES = 3
_UPLOAD_BACKOFF_BASE = 1.0


class SandboxExecutor:
    def __init__(self, config_store: SandboxConfigStore | None = None) -> None:
        self._store = config_store or SandboxConfigStore()
        self._token: str | None = None
        self._token_expires: float = 0
        self._pending_data_zip: bytes | None = None

    @property
    def enabled(self) -> bool:
        return self._store.enabled

    async def pre_sync(self) -> None:
        if not self._store.sync_data:
            self._pending_data_zip = None
            return
        self._pending_data_zip = self._create_data_zip()

    async def post_sync(self) -> int:
        self._pending_data_zip = None
        return 0

    async def execute(
        self,
        command: str,
        *,
        env_vars: dict[str, str] | None = None,
        timeout: int = 300,
    ) -> dict[str, Any]:
        start = time.time()
        session_id = str(uuid.uuid4())

        try:
            token = await self._get_token()
        except Exception as exc:
            return {"success": False, "error": f"Auth failed: {exc}", "session_id": session_id}

        endpoint = self._store.session_pool_endpoint
        if not endpoint:
            return {"success": False, "error": "Session pool endpoint not configured"}

        async with aiohttp.ClientSession() as http:
            headers = {"Authorization": f"Bearer {token}"}

            data_zip = self._create_data_zip() if self._store.sync_data else None
            try:
                code_zip = self._create_code_zip()
            except Exception as exc:
                return self._result(False, f"Failed to create code archive: {exc}", start, session_id)

            if data_zip:
                if not await self._upload_bytes(http, endpoint, session_id, "agent_data.zip", data_zip, headers):
                    return self._result(False, "Data upload failed", start, session_id)

            if not await self._upload_bytes(http, endpoint, session_id, "polyclaw_code.zip", code_zip, headers):
                return self._result(False, "Code upload failed", start, session_id)

            bootstrap = self._build_bootstrap_script(command, has_data=data_zip is not None, env_vars=env_vars)
            if not await self._upload_bytes(http, endpoint, session_id, "bootstrap.sh", bootstrap.encode(), headers):
                return self._result(False, "Bootstrap upload failed", start, session_id)

            exec_result = await self._execute_in_session(http, endpoint, session_id, headers, timeout)
            if not exec_result["success"]:
                return {**exec_result, **self._timing(start, session_id)}

            files_synced = 0
            if self._store.sync_data:
                try:
                    result_zip = await self._download_file(http, endpoint, session_id, "agent_result.zip", headers)
                    if result_zip:
                        files_synced = self._merge_result_zip(result_zip)
                except Exception as exc:
                    logger.warning("Failed to merge sandbox results: %s", exc)

            return {
                "success": True,
                "stdout": exec_result.get("stdout", ""),
                "stderr": exec_result.get("stderr", ""),
                "files_synced_back": files_synced,
                **self._timing(start, session_id),
            }

    def _create_data_zip(self) -> bytes | None:
        data_dir = cfg.data_dir
        whitelist = self._store.whitelist
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item_name in whitelist:
                item_path = data_dir / item_name
                if not item_path.exists():
                    continue
                if item_path.is_file():
                    if buf.tell() + item_path.stat().st_size > MAX_ZIP_SIZE:
                        continue
                    zf.write(item_path, item_name)
                    count += 1
                elif item_path.is_dir():
                    for root, _dirs, files in os.walk(item_path):
                        for fname in files:
                            fpath = Path(root) / fname
                            arcname = str(fpath.relative_to(data_dir))
                            if buf.tell() + fpath.stat().st_size > MAX_ZIP_SIZE:
                                continue
                            zf.write(fpath, arcname)
                            count += 1
        return buf.getvalue() if count else None

    def _create_code_zip(self) -> bytes:
        project_root = cfg.project_root
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for src_dir in ("polyclaw", "app/runtime"):
                full = project_root / src_dir
                if not full.is_dir():
                    continue
                for root, _dirs, files in os.walk(full):
                    root_path = Path(root)
                    if "__pycache__" in root_path.parts:
                        continue
                    for fname in files:
                        if fname.endswith(".pyc"):
                            continue
                        fpath = root_path / fname
                        zf.write(fpath, str(fpath.relative_to(project_root)))

            pyproject = project_root / "pyproject.toml"
            if pyproject.exists():
                zf.write(pyproject, "pyproject.toml")

            for extra in ("skills", "plugins"):
                extra_path = project_root / extra
                if not extra_path.is_dir():
                    continue
                for root, _dirs, files in os.walk(extra_path):
                    root_path = Path(root)
                    if "__pycache__" in root_path.parts:
                        continue
                    for fname in files:
                        fpath = root_path / fname
                        zf.write(fpath, str(fpath.relative_to(project_root)))
        return buf.getvalue()

    def _merge_result_zip(self, zip_data: bytes) -> int:
        data_dir = cfg.data_dir
        whitelist = set(self._store.whitelist)
        count = 0
        with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                top_level = name.split("/")[0]
                if top_level not in whitelist or ".." in name or name.startswith("/"):
                    continue
                dest = data_dir / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                count += 1
        return count

    def _build_bootstrap_script(
        self,
        command: str,
        *,
        has_data: bool,
        env_vars: dict[str, str] | None = None,
    ) -> str:
        lines = ["#!/bin/bash", "set -e", "", "cd /mnt/data", ""]
        lines += ["export HOME=/mnt/data/agent_home", "mkdir -p $HOME", ""]
        if has_data:
            lines += [
                "if [ -f agent_data.zip ]; then",
                '  python3 -c "import zipfile; zipfile.ZipFile(\'agent_data.zip\').extractall(\'$HOME\')"',
                "fi", "",
            ]
        lines += [
            "mkdir -p /mnt/data/polyclaw_src",
            'python3 -c "import zipfile; zipfile.ZipFile(\'polyclaw_code.zip\').extractall(\'/mnt/data/polyclaw_src\')"',
            "", "cd /mnt/data/polyclaw_src",
            "pip install -e . --quiet 2>/dev/null || true",
            "cd /mnt/data", "",
            'export POLYCLAW_DATA_DIR="$HOME"',
        ]
        if env_vars:
            for k, v in env_vars.items():
                lines.append(f"export {k}='{v.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'")
        lines += ["", command, "EXIT_CODE=$?", ""]
        if has_data:
            lines += [
                "cd $HOME",
                "python3 -c \""
                "import zipfile, os, pathlib;"
                "EXCLUDE={'.cache','.azure','.config','.IdentityService','.net','.npm','.pki'};"
                "zf=zipfile.ZipFile('/mnt/data/agent_result.zip','w',zipfile.ZIP_DEFLATED);"
                "[zf.write(os.path.join(r,f),os.path.relpath(os.path.join(r,f))) "
                "for r,_,fs in os.walk('.') "
                "if not any(p in EXCLUDE for p in pathlib.PurePath(r).parts) "
                "for f in fs if not f.endswith('.pyc')];"
                "zf.close()\"", "",
            ]
        lines += ["exit $EXIT_CODE"]
        return "\n".join(lines)

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires - 60:
            return self._token
        from azure.identity import AzureCliCredential, DefaultAzureCredential

        for cred_cls in (AzureCliCredential, DefaultAzureCredential):
            try:
                cred = cred_cls()
                token = cred.get_token(TOKEN_SCOPE)
                self._token = token.token
                self._token_expires = token.expires_on
                return self._token
            except Exception:
                continue
        raise RuntimeError("Failed to acquire Azure credentials for Dynamic Sessions")

    async def _upload_bytes(
        self, http: aiohttp.ClientSession, endpoint: str, session_id: str,
        filename: str, data: bytes, headers: dict[str, str],
    ) -> bool:
        url = f"{endpoint}/files/upload?api-version={API_VERSION}&identifier={session_id}"
        last_error = ""
        for attempt in range(_UPLOAD_MAX_RETRIES):
            form = aiohttp.FormData()
            form.add_field(
                "file", data, filename=filename, content_type="application/octet-stream",
            )
            try:
                async with http.post(
                    url, data=form, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status in (200, 201, 202):
                        return True
                    body = await resp.text()
                    last_error = f"HTTP {resp.status}: {body[:300]}"
                    logger.warning(
                        "Upload %s attempt %d/%d failed: %s",
                        filename, attempt + 1, _UPLOAD_MAX_RETRIES, last_error,
                    )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Upload %s attempt %d/%d error: %s",
                    filename, attempt + 1, _UPLOAD_MAX_RETRIES, exc,
                )
            if attempt < _UPLOAD_MAX_RETRIES - 1:
                delay = _UPLOAD_BACKOFF_BASE * (2 ** attempt)
                await asyncio.sleep(delay)
        logger.error(
            "Upload %s failed after %d attempts: %s",
            filename, _UPLOAD_MAX_RETRIES, last_error,
        )
        return False

    async def _execute_in_session(
        self, http: aiohttp.ClientSession, endpoint: str, session_id: str,
        headers: dict[str, str], timeout: int,
    ) -> dict[str, Any]:
        url = f"{endpoint}/code/execute?api-version={API_VERSION}&identifier={session_id}"
        payload = {
            "properties": {
                "codeInputType": "inline",
                "executionType": "synchronous",
                "code": (
                    "import subprocess, json, sys\n"
                    "r = subprocess.run(['bash', '/mnt/data/bootstrap.sh'], "
                    f"capture_output=True, text=True, timeout={timeout})\n"
                    "print(json.dumps({'stdout': r.stdout, 'stderr': r.stderr, "
                    "'rc': r.returncode}))\n"
                ),
            }
        }
        try:
            async with http.post(
                url, json=payload, headers={**headers, "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout + 30),
            ) as resp:
                if resp.status not in (200, 201, 202):
                    text = await resp.text()
                    return {"success": False, "error": f"Execution failed: {resp.status} {text[:300]}"}
                result = await resp.json()
                props = result.get("properties", {})
                raw_stdout = props.get("stdout", "")
                try:
                    output = json.loads(raw_stdout.strip())
                    stdout, stderr, rc = output.get("stdout", ""), output.get("stderr", ""), output.get("rc", 0)
                except (json.JSONDecodeError, AttributeError, TypeError):
                    stdout, stderr, rc = raw_stdout, props.get("stderr", ""), 1
                if rc != 0:
                    return {"success": False, "stdout": stdout, "stderr": stderr, "error": stderr or f"Exit code {rc}"}
                return {"success": True, "stdout": stdout, "stderr": stderr}
        except Exception as exc:
            logger.error("Sandbox exec exception: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    async def provision_session(self, session_id: str) -> dict[str, Any]:
        start = time.time()
        try:
            token = await self._get_token()
        except Exception as exc:
            return {"success": False, "error": f"Auth failed: {exc}", "session_id": session_id}

        endpoint = self._store.session_pool_endpoint
        if not endpoint:
            return {"success": False, "error": "Session pool endpoint not configured"}

        async with aiohttp.ClientSession() as http:
            headers = {"Authorization": f"Bearer {token}"}

            data_zip = self._create_data_zip() if self._store.sync_data else None
            if data_zip:
                if not await self._upload_bytes(http, endpoint, session_id, "agent_data.zip", data_zip, headers):
                    return self._result(False, "Data upload failed", start, session_id)

            try:
                code_zip = self._create_code_zip()
            except Exception as exc:
                return self._result(False, f"Code archive failed: {exc}", start, session_id)
            if not await self._upload_bytes(http, endpoint, session_id, "polyclaw_code.zip", code_zip, headers):
                return self._result(False, "Code upload failed", start, session_id)

            setup = self._build_bootstrap_script("echo 'Session bootstrapped OK'", has_data=data_zip is not None)
            if not await self._upload_bytes(http, endpoint, session_id, "bootstrap.sh", setup.encode(), headers):
                return self._result(False, "Bootstrap upload failed", start, session_id)

            exec_result = await self._execute_in_session(http, endpoint, session_id, headers, timeout=120)
            if not exec_result["success"]:
                return {**exec_result, **self._timing(start, session_id)}

            return {"success": True, **self._timing(start, session_id)}

    async def run_in_session(self, session_id: str, command: str, *, timeout: int = 120) -> dict[str, Any]:
        start = time.time()
        try:
            token = await self._get_token()
        except Exception as exc:
            return {"success": False, "error": f"Auth failed: {exc}"}

        endpoint = self._store.session_pool_endpoint
        if not endpoint:
            return {"success": False, "error": "Session pool endpoint not configured"}

        async with aiohttp.ClientSession() as http:
            headers = {"Authorization": f"Bearer {token}"}
            return await self._execute_code(http, endpoint, session_id, command, headers, timeout)

    async def _execute_code(
        self, http: aiohttp.ClientSession, endpoint: str, session_id: str,
        command: str, headers: dict[str, str], timeout: int,
    ) -> dict[str, Any]:
        url = f"{endpoint}/code/execute?api-version={API_VERSION}&identifier={session_id}"
        cmd_b64 = base64.b64encode(command.encode()).decode()
        code = (
            "import subprocess, json, base64\n"
            f"cmd = base64.b64decode('{cmd_b64}').decode()\n"
            f"r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout={timeout}, "
            "cwd='/mnt/data/agent_home', "
            "env={**__import__('os').environ, 'HOME': '/mnt/data/agent_home'})\n"
            "print(json.dumps({'stdout': r.stdout, 'stderr': r.stderr, 'rc': r.returncode}))\n"
        )
        payload = {"properties": {"codeInputType": "inline", "executionType": "synchronous", "code": code}}
        try:
            async with http.post(
                url, json=payload, headers={**headers, "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout + 30),
            ) as resp:
                if resp.status not in (200, 201, 202):
                    text = await resp.text()
                    return {"success": False, "error": f"HTTP {resp.status}: {text[:300]}"}
                result = await resp.json()
                raw_stdout = result.get("properties", {}).get("stdout", "")
                try:
                    output = json.loads(raw_stdout.strip())
                    stdout, stderr, rc = output.get("stdout", ""), output.get("stderr", ""), output.get("rc", 0)
                except (json.JSONDecodeError, AttributeError, TypeError):
                    stdout, stderr, rc = raw_stdout, result.get("properties", {}).get("stderr", ""), 1
                if rc != 0:
                    return {"success": False, "stdout": stdout, "stderr": stderr, "error": stderr or f"Exit code {rc}"}
                return {"success": True, "stdout": stdout, "stderr": stderr}
        except Exception as exc:
            logger.error("Session exec exception: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    async def destroy_session(self, session_id: str) -> None:
        if self._store.sync_data:
            try:
                token = await self._get_token()
                endpoint = self._store.session_pool_endpoint
                if endpoint:
                    async with aiohttp.ClientSession() as http:
                        headers = {"Authorization": f"Bearer {token}"}
                        zip_data = await self._download_file(http, endpoint, session_id, "agent_result.zip", headers)
                        if zip_data:
                            self._merge_result_zip(zip_data)
            except Exception as exc:
                logger.warning("Session teardown sync failed: %s", exc)

    async def _download_file(
        self, http: aiohttp.ClientSession, endpoint: str, session_id: str,
        filename: str, headers: dict[str, str],
    ) -> bytes | None:
        url = f"{endpoint}/files/content/{filename}?api-version={API_VERSION}&identifier={session_id}"
        try:
            async with http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                return await resp.read() if resp.status == 200 else None
        except Exception as exc:
            logger.warning("Download %s failed: %s", filename, exc)
            return None

    def _timing(self, start: float, session_id: str) -> dict[str, Any]:
        return {"duration_ms": round((time.time() - start) * 1000), "session_id": session_id}

    def _result(self, success: bool, error: str, start: float, session_id: str) -> dict[str, Any]:
        return {"success": success, "error": error, **self._timing(start, session_id)}


class SandboxToolInterceptor:
    def __init__(self, executor: SandboxExecutor) -> None:
        self._executor = executor
        self._session_id: str | None = None
        self._session_ready: bool = False
        self._provisioning: bool = False
        self._last_activity: float = 0
        self._idle_task: asyncio.Task | None = None
        self._pending_result: dict[str, Any] | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def _ensure_session(self) -> str:
        self._last_activity = time.time()
        if self._session_id and self._session_ready:
            return self._session_id

        self._session_id = str(uuid.uuid4())
        self._session_ready = False
        self._provisioning = True

        try:
            result = await self._executor.provision_session(self._session_id)
            if not result["success"]:
                self._session_id = None
                raise RuntimeError(f"Sandbox session provision failed: {result.get('error')}")
            self._session_ready = True
        finally:
            self._provisioning = False

        self._start_idle_timer()
        return self._session_id

    async def _teardown_session(self) -> None:
        if not self._session_id:
            return
        sid = self._session_id
        self._session_id = None
        self._session_ready = False
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None
        try:
            await self._executor.destroy_session(sid)
        except Exception as exc:
            logger.warning("Session teardown error: %s", exc)

    def _start_idle_timer(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.ensure_future(self._idle_reaper())

    async def _idle_reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                if not self._session_id:
                    return
                if time.time() - self._last_activity >= _SESSION_IDLE_TIMEOUT:
                    await self._teardown_session()
                    return
        except asyncio.CancelledError:
            pass

    def touch(self) -> None:
        self._last_activity = time.time()

    async def on_pre_tool_use(self, input_data: dict, ctx: dict) -> dict | None:
        tool_name = input_data.get("toolName", "")
        if not self._executor.enabled:
            return {"permissionDecision": "allow"}
        if not _is_shell_tool(tool_name):
            return {"permissionDecision": "allow"}

        tool_args = _parse_tool_args(input_data.get("toolArgs"))
        command = _extract_command(tool_args)
        if not command:
            return {"permissionDecision": "allow"}

        try:
            session_id = await self._ensure_session()
            result = await self._executor.run_in_session(session_id, command, timeout=120)
            self._last_activity = time.time()
        except Exception as exc:
            logger.error("Sandbox interceptor failed: %s", exc, exc_info=True)
            result = {"success": False, "stdout": "", "stderr": str(exc)}

        self._pending_result = result
        replay = _build_replay_command(
            result.get("stdout", ""), result.get("stderr", ""), result.get("success", False)
        )
        noop_args = dict(tool_args)
        noop_args["command"] = replay
        if "input" in noop_args:
            noop_args["input"] = replay
        return {"permissionDecision": "allow", "modifiedArgs": noop_args}

    async def on_post_tool_use(self, input_data: dict, ctx: dict) -> dict | None:
        if self._pending_result is None:
            return None

        result = self._pending_result
        self._pending_result = None

        parts: list[str] = []
        if result.get("stdout"):
            parts.append(result["stdout"])
        if result.get("stderr"):
            parts.append(f"STDERR:\n{result['stderr']}")
        output = "\n".join(parts) if parts else "(no output)"
        if not result.get("success"):
            output = f"Command failed in sandbox.\n{output}"
        return {"modifiedResult": output}


def _parse_tool_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _extract_command(args: Any) -> str:
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                args = parsed
            else:
                return args
        except (json.JSONDecodeError, TypeError):
            return args
    if isinstance(args, dict):
        return args.get("command", "") or args.get("cmd", "") or args.get("input", "") or args.get("script", "")
    return ""


def _is_shell_tool(name: str) -> bool:
    lower = name.lower()
    return any(p in lower for p in _SHELL_TOOL_PATTERNS)


def _build_replay_command(stdout: str, stderr: str, success: bool) -> str:
    parts: list[str] = []
    if stdout:
        parts.append(f"printf %s {shlex.quote(stdout)}")
    if stderr:
        parts.append(f"printf %s {shlex.quote(stderr)} >&2")
    if not success:
        parts.append("exit 1")
    return " ; ".join(parts) if parts else "true"
