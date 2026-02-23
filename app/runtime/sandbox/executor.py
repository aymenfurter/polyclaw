"""Sandbox executor -- runs agent commands in ACA Dynamic Sessions."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import aiohttp

from ..config.settings import cfg
from ..state.sandbox_config import SandboxConfigStore

logger = logging.getLogger(__name__)

API_VERSION = "2024-02-02-preview"
TOKEN_SCOPE = "https://dynamicsessions.io/.default"
MAX_ZIP_SIZE = 100 * 1024 * 1024

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
                err = await self._upload_bytes(http, endpoint, session_id, "agent_data.zip", data_zip, headers)
                if err:
                    return self._result(False, f"Data upload failed: {err}", start, session_id)

            err = await self._upload_bytes(http, endpoint, session_id, "polyclaw_code.zip", code_zip, headers)
            if err:
                return self._result(False, f"Code upload failed: {err}", start, session_id)

            bootstrap = self._build_bootstrap_script(command, has_data=data_zip is not None, env_vars=env_vars)
            err = await self._upload_bytes(http, endpoint, session_id, "bootstrap.sh", bootstrap.encode(), headers)
            if err:
                return self._result(False, f"Bootstrap upload failed: {err}", start, session_id)

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
    ) -> str:
        """Upload bytes to the session. Returns empty string on success, error detail on failure."""
        url = f"{endpoint}/files/upload?api-version={API_VERSION}&identifier={session_id}"
        size_kb = len(data) / 1024
        logger.info(
            "[sandbox.upload] file=%s size=%.1fKB session=%s",
            filename, size_kb, session_id,
        )
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
                        return ""
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
        return last_error

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
                return self._parse_exec_result(
                    props.get("stdout", ""),
                    fallback_stderr=props.get("stderr", ""),
                )
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
            has_data = False
            if data_zip:
                err = await self._upload_bytes(http, endpoint, session_id, "agent_data.zip", data_zip, headers)
                if err:
                    logger.warning(
                        "[sandbox.provision] Data upload failed (non-fatal), "
                        "continuing without data sync: %s", err,
                    )
                else:
                    has_data = True

            try:
                code_zip = self._create_code_zip()
            except Exception as exc:
                return self._result(False, f"Code archive failed: {exc}", start, session_id)
            err = await self._upload_bytes(http, endpoint, session_id, "polyclaw_code.zip", code_zip, headers)
            if err:
                return self._result(False, f"Code upload failed: {err}", start, session_id)

            setup = self._build_bootstrap_script("echo 'Session bootstrapped OK'", has_data=has_data)
            err = await self._upload_bytes(http, endpoint, session_id, "bootstrap.sh", setup.encode(), headers)
            if err:
                return self._result(False, f"Bootstrap upload failed: {err}", start, session_id)

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
                props = result.get("properties", {})
                return self._parse_exec_result(
                    props.get("stdout", ""),
                    fallback_stderr=props.get("stderr", ""),
                )
        except Exception as exc:
            logger.error("Session exec exception: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    @staticmethod
    def _parse_exec_result(
        raw_stdout: str, fallback_stderr: str = "",
    ) -> dict[str, Any]:
        """Parse JSON-wrapped subprocess output into a result dict."""
        try:
            output = json.loads(raw_stdout.strip())
            stdout = output.get("stdout", "")
            stderr = output.get("stderr", "")
            rc = output.get("rc", 0)
        except (json.JSONDecodeError, AttributeError, TypeError):
            stdout, stderr, rc = raw_stdout, fallback_stderr, 1
        if rc != 0:
            return {
                "success": False,
                "stdout": stdout,
                "stderr": stderr,
                "error": stderr or f"Exit code {rc}",
            }
        return {"success": True, "stdout": stdout, "stderr": stderr}

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
