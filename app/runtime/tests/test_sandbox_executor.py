"""Tests for sandbox executor pure functions and helper methods."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from app.runtime.sandbox import (
    SandboxExecutor,
    SandboxToolInterceptor,
    _build_replay_command,
    _extract_command,
    _is_shell_tool,
    _parse_tool_args,
)


class TestIsShellTool:
    def test_terminal(self) -> None:
        assert _is_shell_tool("run_terminal")

    def test_shell(self) -> None:
        assert _is_shell_tool("execute_shell")

    def test_bash(self) -> None:
        assert _is_shell_tool("bash_runner")

    def test_command(self) -> None:
        assert _is_shell_tool("run_command")

    def test_non_shell(self) -> None:
        assert not _is_shell_tool("search_web")
        assert not _is_shell_tool("read_file")
        assert not _is_shell_tool("list_files")

    def test_case_insensitive(self) -> None:
        assert _is_shell_tool("RunTerminal")
        assert _is_shell_tool("BASH_EXEC")


class TestParseToolArgs:
    def test_dict_passthrough(self) -> None:
        assert _parse_tool_args({"cmd": "ls"}) == {"cmd": "ls"}

    def test_json_string(self) -> None:
        assert _parse_tool_args('{"command": "ls"}') == {"command": "ls"}

    def test_invalid_json(self) -> None:
        assert _parse_tool_args("not json") == {}

    def test_non_dict_json(self) -> None:
        assert _parse_tool_args("[1, 2, 3]") == {}

    def test_none(self) -> None:
        assert _parse_tool_args(None) == {}

    def test_integer(self) -> None:
        assert _parse_tool_args(42) == {}


class TestExtractCommand:
    def test_from_dict_command(self) -> None:
        assert _extract_command({"command": "ls -la"}) == "ls -la"

    def test_from_dict_cmd(self) -> None:
        assert _extract_command({"cmd": "pwd"}) == "pwd"

    def test_from_dict_input(self) -> None:
        assert _extract_command({"input": "echo hello"}) == "echo hello"

    def test_from_dict_script(self) -> None:
        assert _extract_command({"script": "#!/bin/bash"}) == "#!/bin/bash"

    def test_from_string(self) -> None:
        assert _extract_command("ls -la") == "ls -la"

    def test_from_json_string(self) -> None:
        assert _extract_command('{"command": "ls"}') == "ls"

    def test_empty_dict(self) -> None:
        assert _extract_command({}) == ""

    def test_priority(self) -> None:
        assert _extract_command({"command": "first", "cmd": "second"}) == "first"


class TestBuildReplayCommand:
    def test_stdout_only(self) -> None:
        cmd = _build_replay_command("hello", "", True)
        assert "printf" in cmd
        assert "hello" in cmd

    def test_stderr(self) -> None:
        cmd = _build_replay_command("", "error msg", True)
        assert ">&2" in cmd

    def test_failure(self) -> None:
        cmd = _build_replay_command("out", "err", False)
        assert "exit 1" in cmd

    def test_empty(self) -> None:
        assert _build_replay_command("", "", True) == "true"

    def test_both_outputs_success(self) -> None:
        cmd = _build_replay_command("out", "err", True)
        assert "printf" in cmd
        assert "exit 1" not in cmd


class TestSandboxExecutor:
    def test_enabled_delegates_to_store(self) -> None:
        store = MagicMock()
        store.enabled = True
        executor = SandboxExecutor(config_store=store)
        assert executor.enabled is True

    def test_not_enabled(self) -> None:
        store = MagicMock()
        store.enabled = False
        executor = SandboxExecutor(config_store=store)
        assert executor.enabled is False

    def test_build_bootstrap_basic(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)
        script = executor._build_bootstrap_script("echo hello", has_data=False)
        assert "#!/bin/bash" in script
        assert "echo hello" in script
        assert "set -e" in script
        assert "agent_data.zip" not in script

    def test_build_bootstrap_with_data(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)
        script = executor._build_bootstrap_script("echo hello", has_data=True)
        assert "agent_data.zip" in script
        assert "agent_result.zip" in script

    def test_build_bootstrap_with_env(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)
        script = executor._build_bootstrap_script(
            "echo hello", has_data=False, env_vars={"MY_VAR": "value"}
        )
        assert "MY_VAR" in script
        assert "value" in script

    def test_create_data_zip_empty(self, tmp_path: Path) -> None:
        store = MagicMock()
        store.whitelist = ["nonexistent"]
        executor = SandboxExecutor(config_store=store)
        with patch("app.runtime.sandbox.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            result = executor._create_data_zip()
        assert result is None

    def test_create_data_zip_with_file(self, tmp_path: Path) -> None:
        (tmp_path / "test.json").write_text('{"key": "val"}')
        store = MagicMock()
        store.whitelist = ["test.json"]
        executor = SandboxExecutor(config_store=store)
        with patch("app.runtime.sandbox.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            result = executor._create_data_zip()
        assert result is not None
        with zipfile.ZipFile(io.BytesIO(result), "r") as zf:
            assert "test.json" in zf.namelist()

    def test_create_data_zip_with_dir(self, tmp_path: Path) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "a.txt").write_text("hello")
        store = MagicMock()
        store.whitelist = ["subdir"]
        executor = SandboxExecutor(config_store=store)
        with patch("app.runtime.sandbox.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            result = executor._create_data_zip()
        assert result is not None
        with zipfile.ZipFile(io.BytesIO(result), "r") as zf:
            assert any("a.txt" in n for n in zf.namelist())

    def test_merge_result_zip(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("allowed/data.txt", "result data")
            zf.writestr("disallowed/bad.txt", "nope")
        store = MagicMock()
        store.whitelist = ["allowed"]
        executor = SandboxExecutor(config_store=store)
        with patch("app.runtime.sandbox.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            count = executor._merge_result_zip(buf.getvalue())
        assert count == 1
        assert (tmp_path / "allowed" / "data.txt").read_text() == "result data"
        assert not (tmp_path / "disallowed").exists()

    def test_merge_result_zip_blocks_path_traversal(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escape.txt", "bad")
            zf.writestr("/absolute.txt", "bad")
        store = MagicMock()
        store.whitelist = []
        executor = SandboxExecutor(config_store=store)
        with patch("app.runtime.sandbox.cfg") as mock_cfg:
            mock_cfg.data_dir = tmp_path
            count = executor._merge_result_zip(buf.getvalue())
        assert count == 0

    def test_timing(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)
        import time
        start = time.time()
        t = executor._timing(start, "sess-1")
        assert "duration_ms" in t
        assert "session_id" in t
        assert t["session_id"] == "sess-1"

    def test_result_helper(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)
        import time
        start = time.time()
        r = executor._result(False, "oops", start, "sess-2")
        assert r["success"] is False
        assert r["error"] == "oops"
        assert "duration_ms" in r

    @pytest.mark.asyncio
    async def test_pre_sync_no_data(self) -> None:
        store = MagicMock()
        store.sync_data = False
        executor = SandboxExecutor(config_store=store)
        await executor.pre_sync()
        assert executor._pending_data_zip is None

    @pytest.mark.asyncio
    async def test_post_sync(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)
        executor._pending_data_zip = b"data"
        count = await executor.post_sync()
        assert count == 0
        assert executor._pending_data_zip is None

    @pytest.mark.asyncio
    async def test_execute_no_endpoint(self) -> None:
        store = MagicMock()
        store.session_pool_endpoint = ""
        store.sync_data = False
        executor = SandboxExecutor(config_store=store)
        with patch.object(executor, "_get_token", new_callable=AsyncMock, return_value="tok"):
            result = await executor.execute("echo hi")
        assert result["success"] is False
        assert "endpoint" in result["error"].lower()


class TestSandboxToolInterceptor:
    def test_session_id_initially_none(self) -> None:
        executor = MagicMock()
        interceptor = SandboxToolInterceptor(executor)
        assert interceptor.session_id is None

    def test_touch_updates_last_activity(self) -> None:
        executor = MagicMock()
        interceptor = SandboxToolInterceptor(executor)
        interceptor.touch()
        assert interceptor._last_activity > 0

    @pytest.mark.asyncio
    async def test_on_pre_tool_use_disabled(self) -> None:
        executor = MagicMock()
        executor.enabled = False
        interceptor = SandboxToolInterceptor(executor)
        result = await interceptor.on_pre_tool_use({"toolName": "run_terminal"}, {})
        assert result == {"permissionDecision": "allow"}

    @pytest.mark.asyncio
    async def test_on_pre_tool_use_non_shell(self) -> None:
        executor = MagicMock()
        executor.enabled = True
        interceptor = SandboxToolInterceptor(executor)
        result = await interceptor.on_pre_tool_use({"toolName": "search_web"}, {})
        assert result == {"permissionDecision": "allow"}

    @pytest.mark.asyncio
    async def test_on_pre_tool_use_empty_command(self) -> None:
        executor = MagicMock()
        executor.enabled = True
        interceptor = SandboxToolInterceptor(executor)
        result = await interceptor.on_pre_tool_use(
            {"toolName": "run_terminal", "toolArgs": {"command": ""}}, {}
        )
        assert result == {"permissionDecision": "allow"}

    @pytest.mark.asyncio
    async def test_on_post_tool_use_no_pending(self) -> None:
        executor = MagicMock()
        interceptor = SandboxToolInterceptor(executor)
        result = await interceptor.on_post_tool_use({}, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_on_post_tool_use_with_pending(self) -> None:
        executor = MagicMock()
        interceptor = SandboxToolInterceptor(executor)
        interceptor._pending_result = {
            "success": True, "stdout": "hello world", "stderr": ""
        }
        result = await interceptor.on_post_tool_use({}, {})
        assert result is not None
        assert "hello world" in result["modifiedResult"]
        assert interceptor._pending_result is None

    @pytest.mark.asyncio
    async def test_on_post_tool_use_failure(self) -> None:
        executor = MagicMock()
        interceptor = SandboxToolInterceptor(executor)
        interceptor._pending_result = {
            "success": False, "stdout": "", "stderr": "boom"
        }
        result = await interceptor.on_post_tool_use({}, {})
        assert "failed" in result["modifiedResult"].lower()

    @pytest.mark.asyncio
    async def test_on_post_tool_use_no_output(self) -> None:
        executor = MagicMock()
        interceptor = SandboxToolInterceptor(executor)
        interceptor._pending_result = {"success": True, "stdout": "", "stderr": ""}
        result = await interceptor.on_post_tool_use({}, {})
        assert "(no output)" in result["modifiedResult"]


class TestUploadBytesRetry:
    """Tests for _upload_bytes retry logic with exponential backoff."""

    @pytest.mark.asyncio
    @patch("app.runtime.sandbox._UPLOAD_BACKOFF_BASE", 0.0)
    async def test_upload_succeeds_on_first_attempt(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        http = MagicMock(spec=aiohttp.ClientSession)
        http.post = MagicMock(return_value=mock_resp)

        result = await executor._upload_bytes(
            http, "https://endpoint", "sess-1", "file.zip", b"data", {},
        )
        assert result is True
        assert http.post.call_count == 1

    @pytest.mark.asyncio
    @patch("app.runtime.sandbox._UPLOAD_BACKOFF_BASE", 0.0)
    async def test_upload_retries_on_http_error_then_succeeds(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)

        fail_resp = AsyncMock()
        fail_resp.status = 500
        fail_resp.text = AsyncMock(return_value="Internal Server Error")
        fail_resp.__aenter__ = AsyncMock(return_value=fail_resp)
        fail_resp.__aexit__ = AsyncMock(return_value=False)

        ok_resp = AsyncMock()
        ok_resp.status = 200
        ok_resp.__aenter__ = AsyncMock(return_value=ok_resp)
        ok_resp.__aexit__ = AsyncMock(return_value=False)

        http = MagicMock(spec=aiohttp.ClientSession)
        http.post = MagicMock(side_effect=[fail_resp, ok_resp])

        result = await executor._upload_bytes(
            http, "https://endpoint", "sess-1", "file.zip", b"data", {},
        )
        assert result is True
        assert http.post.call_count == 2

    @pytest.mark.asyncio
    @patch("app.runtime.sandbox._UPLOAD_BACKOFF_BASE", 0.0)
    async def test_upload_retries_on_exception_then_succeeds(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)

        ok_resp = AsyncMock()
        ok_resp.status = 201
        ok_resp.__aenter__ = AsyncMock(return_value=ok_resp)
        ok_resp.__aexit__ = AsyncMock(return_value=False)

        http = MagicMock(spec=aiohttp.ClientSession)
        http.post = MagicMock(
            side_effect=[aiohttp.ClientError("connection reset"), ok_resp],
        )

        result = await executor._upload_bytes(
            http, "https://endpoint", "sess-1", "data.zip", b"data", {},
        )
        assert result is True
        assert http.post.call_count == 2

    @pytest.mark.asyncio
    @patch("app.runtime.sandbox._UPLOAD_BACKOFF_BASE", 0.0)
    async def test_upload_fails_after_all_retries(self) -> None:
        store = MagicMock()
        executor = SandboxExecutor(config_store=store)

        fail_resp = AsyncMock()
        fail_resp.status = 503
        fail_resp.text = AsyncMock(return_value="Service Unavailable")
        fail_resp.__aenter__ = AsyncMock(return_value=fail_resp)
        fail_resp.__aexit__ = AsyncMock(return_value=False)

        http = MagicMock(spec=aiohttp.ClientSession)
        http.post = MagicMock(return_value=fail_resp)

        result = await executor._upload_bytes(
            http, "https://endpoint", "sess-1", "file.zip", b"data", {},
        )
        assert result is False
        assert http.post.call_count == 3
