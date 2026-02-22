"""Tests for the single-command CLI (app.cli.run)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cli.run import _build_parser, _resolve_prompt, _run, _wire_subsystems


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def parser():
    return _build_parser()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_positional_prompt(self, parser):
        args = parser.parse_args(["hello world"])
        assert args.prompt == "hello world"
        assert args.file is None
        assert args.auto_approve is False
        assert args.skip_memory is False
        assert args.quiet is False

    def test_file_flag(self, parser):
        args = parser.parse_args(["--file", "tasks.md"])
        assert args.file == "tasks.md"
        assert args.prompt is None

    def test_auto_approve_flag(self, parser):
        args = parser.parse_args(["--auto-approve", "do stuff"])
        assert args.auto_approve is True

    def test_skip_memory_flag(self, parser):
        args = parser.parse_args(["--skip-memory", "do stuff"])
        assert args.skip_memory is True

    def test_quiet_flag(self, parser):
        args = parser.parse_args(["-q", "do stuff"])
        assert args.quiet is True

    def test_model_override(self, parser):
        args = parser.parse_args(["--model", "gpt-4.1", "prompt"])
        assert args.model == "gpt-4.1"

    def test_stdin_dash(self, parser):
        args = parser.parse_args(["-"])
        assert args.prompt == "-"


# ---------------------------------------------------------------------------
# Prompt resolution
# ---------------------------------------------------------------------------


class TestResolvePrompt:
    def test_from_positional_arg(self, parser):
        args = parser.parse_args(["hello agent"])
        assert _resolve_prompt(args) == "hello agent"

    def test_from_file(self, parser, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("summarize things")
        args = parser.parse_args(["--file", str(f)])
        assert _resolve_prompt(args) == "summarize things"

    def test_file_not_found(self, parser):
        args = parser.parse_args(["--file", "/nonexistent/prompt.txt"])
        with pytest.raises(SystemExit):
            _resolve_prompt(args)

    def test_no_prompt_given(self, parser):
        args = parser.parse_args([])
        with pytest.raises(SystemExit):
            _resolve_prompt(args)

    def test_stdin_dash_on_tty_exits(self, parser, monkeypatch):
        args = parser.parse_args(["-"])
        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: True))
        with pytest.raises(SystemExit):
            _resolve_prompt(args)


# ---------------------------------------------------------------------------
# Subsystem wiring
# ---------------------------------------------------------------------------


class TestWireSubsystems:
    @patch("app.cli.run.SandboxConfigStore")
    @patch("app.cli.run.GuardrailsConfigStore")
    def test_wires_guardrails(self, mock_guardrails_cls, mock_sandbox_cls):
        agent = MagicMock()
        agent.hitl_interceptor = MagicMock()

        mock_sandbox = MagicMock()
        mock_sandbox.enabled = False
        mock_sandbox_cls.return_value = mock_sandbox

        _wire_subsystems(agent, auto_approve=False)

        agent.set_guardrails.assert_called_once()
        agent.hitl_interceptor.set_emit.assert_called_once()

    @patch("app.cli.run.SandboxConfigStore")
    @patch("app.cli.run.GuardrailsConfigStore")
    def test_auto_approve_skips_emit(self, mock_guardrails_cls, mock_sandbox_cls):
        agent = MagicMock()
        agent.hitl_interceptor = MagicMock()

        mock_sandbox = MagicMock()
        mock_sandbox.enabled = False
        mock_sandbox_cls.return_value = mock_sandbox

        _wire_subsystems(agent, auto_approve=True)

        agent.set_guardrails.assert_called_once()
        agent.hitl_interceptor.set_emit.assert_not_called()


# ---------------------------------------------------------------------------
# Full run (mocked agent)
# ---------------------------------------------------------------------------


class TestRun:
    @patch("app.cli.run.SessionStore")
    @patch("app.cli.run.get_memory")
    @patch("app.cli.run._wire_subsystems")
    @patch("app.cli.run.Agent")
    async def test_basic_run(
        self, mock_agent_cls, mock_wire, mock_get_memory, mock_session_store_cls,
    ):
        mock_agent = AsyncMock()
        mock_agent.send.return_value = "The answer is 42."
        mock_agent_cls.return_value = mock_agent

        mock_memory = MagicMock()
        mock_memory.record = MagicMock()
        mock_memory.force_form = AsyncMock(return_value={"status": "ok"})
        mock_get_memory.return_value = mock_memory

        mock_sessions = MagicMock()
        mock_session_store_cls.return_value = mock_sessions

        parser = _build_parser()
        args = parser.parse_args(["-q", "What is the meaning of life?"])

        code = await _run(args)

        assert code == 0
        mock_agent.start.assert_awaited_once()
        mock_agent.send.assert_awaited_once()
        mock_agent.stop.assert_awaited_once()
        mock_memory.record.assert_any_call("user", "What is the meaning of life?")
        mock_memory.record.assert_any_call("assistant", "The answer is 42.")
        mock_memory.force_form.assert_awaited_once()

    @patch("app.cli.run.SessionStore")
    @patch("app.cli.run.get_memory")
    @patch("app.cli.run._wire_subsystems")
    @patch("app.cli.run.Agent")
    async def test_skip_memory(
        self, mock_agent_cls, mock_wire, mock_get_memory, mock_session_store_cls,
    ):
        mock_agent = AsyncMock()
        mock_agent.send.return_value = "done"
        mock_agent_cls.return_value = mock_agent

        mock_memory = MagicMock()
        mock_memory.record = MagicMock()
        mock_memory.force_form = AsyncMock()
        mock_get_memory.return_value = mock_memory

        mock_session_store_cls.return_value = MagicMock()

        parser = _build_parser()
        args = parser.parse_args(["-q", "--skip-memory", "hello"])

        code = await _run(args)

        assert code == 0
        mock_memory.force_form.assert_not_awaited()

    @patch("app.cli.run.SessionStore")
    @patch("app.cli.run.get_memory")
    @patch("app.cli.run._wire_subsystems")
    @patch("app.cli.run.Agent")
    async def test_no_response_returns_exit_1(
        self, mock_agent_cls, mock_wire, mock_get_memory, mock_session_store_cls,
    ):
        mock_agent = AsyncMock()
        mock_agent.send.return_value = None
        mock_agent_cls.return_value = mock_agent

        mock_memory = MagicMock()
        mock_memory.record = MagicMock()
        mock_memory.force_form = AsyncMock(return_value={"status": "no_turns"})
        mock_get_memory.return_value = mock_memory

        mock_session_store_cls.return_value = MagicMock()

        parser = _build_parser()
        args = parser.parse_args(["-q", "--skip-memory", "hello"])

        code = await _run(args)

        assert code == 1

    @patch("app.cli.run.SessionStore")
    @patch("app.cli.run.get_memory")
    @patch("app.cli.run._wire_subsystems")
    @patch("app.cli.run.Agent")
    async def test_model_override(
        self, mock_agent_cls, mock_wire, mock_get_memory, mock_session_store_cls,
    ):
        mock_agent = AsyncMock()
        mock_agent.send.return_value = "ok"
        mock_agent_cls.return_value = mock_agent

        mock_memory = MagicMock()
        mock_memory.record = MagicMock()
        mock_memory.force_form = AsyncMock(return_value={"status": "ok"})
        mock_get_memory.return_value = mock_memory

        mock_sessions = MagicMock()
        mock_session_store_cls.return_value = mock_sessions

        parser = _build_parser()
        args = parser.parse_args(["-q", "--model", "gpt-4.1", "hello"])

        code = await _run(args)

        assert code == 0
        # Verify the session store was started with the overridden model
        mock_sessions.start_session.assert_called_once()
        call_kwargs = mock_sessions.start_session.call_args
        assert call_kwargs[1]["model"] == "gpt-4.1" or call_kwargs[0][1] == "gpt-4.1"

    @patch("app.cli.run.SessionStore")
    @patch("app.cli.run.get_memory")
    @patch("app.cli.run._wire_subsystems")
    @patch("app.cli.run.Agent")
    async def test_agent_error_returns_exit_1(
        self, mock_agent_cls, mock_wire, mock_get_memory, mock_session_store_cls,
    ):
        mock_agent = AsyncMock()
        mock_agent.send.side_effect = RuntimeError("SDK crash")
        mock_agent_cls.return_value = mock_agent

        mock_memory = MagicMock()
        mock_memory.record = MagicMock()
        mock_get_memory.return_value = mock_memory

        mock_session_store_cls.return_value = MagicMock()

        parser = _build_parser()
        args = parser.parse_args(["-q", "--skip-memory", "hello"])

        code = await _run(args)

        assert code == 1
        mock_agent.stop.assert_awaited_once()

    @patch("app.cli.run.SessionStore")
    @patch("app.cli.run.get_memory")
    @patch("app.cli.run._wire_subsystems")
    @patch("app.cli.run.Agent")
    async def test_file_prompt(
        self, mock_agent_cls, mock_wire, mock_get_memory, mock_session_store_cls,
        tmp_path,
    ):
        f = tmp_path / "task.md"
        f.write_text("Deploy the database")

        mock_agent = AsyncMock()
        mock_agent.send.return_value = "Deployed."
        mock_agent_cls.return_value = mock_agent

        mock_memory = MagicMock()
        mock_memory.record = MagicMock()
        mock_memory.force_form = AsyncMock(return_value={"status": "ok"})
        mock_get_memory.return_value = mock_memory

        mock_session_store_cls.return_value = MagicMock()

        parser = _build_parser()
        args = parser.parse_args(["-q", "--file", str(f)])

        code = await _run(args)

        assert code == 0
        mock_agent.send.assert_awaited_once()
        # Verify the prompt text came from the file
        call_args = mock_agent.send.call_args
        assert call_args[0][0] == "Deploy the database"


# ---------------------------------------------------------------------------
# TTY approval
# ---------------------------------------------------------------------------


class TestTtyApprove:
    @patch("app.cli.approve._read_yn", return_value=True)
    async def test_approve_yes(self, mock_read):
        from app.cli.approve import tty_approve

        result = await tty_approve(
            {"toolName": "shell", "toolArgs": "ls -la"},
            None,
        )
        assert result == {"permissionDecision": "allow"}

    @patch("app.cli.approve._read_yn", return_value=False)
    async def test_approve_no(self, mock_read):
        from app.cli.approve import tty_approve

        result = await tty_approve(
            {"toolName": "shell", "toolArgs": "rm -rf /"},
            None,
        )
        assert result == {"permissionDecision": "deny"}

    def test_read_yn_non_tty(self, monkeypatch):
        from app.cli.approve import _read_yn

        monkeypatch.setattr("sys.stdin", MagicMock(isatty=lambda: False))
        assert _read_yn() is False
