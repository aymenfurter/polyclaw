"""Single-command CLI entry point.

Spins up the agent, executes a prompt, runs memory post-processing, and
exits.  Designed for scriptable, non-interactive use while still
honouring guardrails, memory, and the local workspace.

Usage::

    polyclaw-run "Summarize my calendar for today"
    polyclaw-run --file tasks.md
    echo "List open PRs" | polyclaw-run -
    polyclaw-run --auto-approve "Refactor the utils module"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

from app.runtime.agent.agent import Agent
from app.runtime.config.settings import cfg
from app.runtime.state.guardrails_config import GuardrailsConfigStore
from app.runtime.state.memory import get_memory
from app.runtime.state.sandbox_config import SandboxConfigStore
from app.runtime.state.session_store import SessionStore

from .approve import tty_approve

logger = logging.getLogger(__name__)
console = Console()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polyclaw-run",
        description="Execute a single Polyclaw task and exit.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help=(
            "The prompt to send to the agent.  "
            "Use '-' to read from stdin."
        ),
    )
    parser.add_argument(
        "-f", "--file",
        type=str,
        default=None,
        help="Read the prompt from a file.",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        default=False,
        help="Auto-approve all tool calls (skip guardrail prompts).",
    )
    parser.add_argument(
        "--skip-memory",
        action="store_true",
        default=False,
        help="Skip memory post-processing after the run.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the model (default: from COPILOT_MODEL env / config).",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        default=False,
        help="Suppress streaming output; only print the final response.",
    )
    return parser


def _resolve_prompt(args: argparse.Namespace) -> str:
    """Return the prompt string from args, file, or stdin."""
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            console.print(f"[red]Error:[/red] file not found: {path}")
            sys.exit(1)
        return path.read_text().strip()

    if args.prompt == "-":
        if sys.stdin.isatty():
            console.print("[red]Error:[/red] stdin is a TTY but '-' was specified. Pipe input or use a prompt argument.")
            sys.exit(1)
        return sys.stdin.read().strip()

    if args.prompt:
        return args.prompt

    console.print("[red]Error:[/red] no prompt provided. Use a positional argument, --file, or pipe to stdin with '-'.")
    sys.exit(1)


def _wire_subsystems(agent: Agent, *, auto_approve: bool) -> None:
    """Attach guardrails, sandbox, and memory to the agent."""
    # Guardrails
    guardrails = GuardrailsConfigStore()
    agent.set_guardrails(guardrails)

    if not auto_approve and agent.hitl_interceptor:
        # Wire the TTY-based emitter so interactive approval works in
        # the terminal.  The emitter callback signature matches what
        # HitlInterceptor.set_emit() expects: (event_name, payload).
        def _cli_emit(event: str, payload: dict[str, Any]) -> None:
            if event == "approval_request":
                # The actual approval prompt is handled by the hook
                # itself when we use tty_approve as the pre-tool hook.
                # For informational events we just log them.
                pass
            elif event == "tool_denied":
                console.print(
                    f"[red]Tool denied:[/red] {payload.get('tool', '?')} "
                    f"-- {payload.get('reason', '')}"
                )
            elif event == "approval_resolved":
                label = "[green]approved[/green]" if payload.get("approved") else "[red]denied[/red]"
                console.print(f"Tool {payload.get('tool', '?')} {label}")

        agent.hitl_interceptor.set_emit(_cli_emit)

    # Sandbox
    try:
        sandbox_cfg = SandboxConfigStore()
        if sandbox_cfg.enabled:
            from app.runtime.sandbox import SandboxExecutor

            executor = SandboxExecutor(sandbox_cfg)
            agent.set_sandbox(executor)
            logger.info("[cli.wire] sandbox enabled")
    except Exception:
        logger.debug("[cli.wire] sandbox not available", exc_info=True)


async def _run(args: argparse.Namespace) -> int:
    """Core async flow: start agent, send prompt, form memory, stop."""
    cfg.ensure_dirs()

    if args.model:
        cfg.copilot_model = args.model

    prompt = _resolve_prompt(args)

    console.print("[bold green]polyclaw-run[/bold green] single-command mode\n")

    # -- Set up agent -------------------------------------------------------
    agent = Agent()
    await agent.start()
    _wire_subsystems(agent, auto_approve=args.auto_approve)

    memory = get_memory()
    session_store = SessionStore()

    exit_code = 0
    try:
        # Record user message
        memory.record("user", prompt)
        session_id = uuid.uuid4().hex[:12]
        session_store.start_session(session_id, model=cfg.copilot_model)
        session_store.record("user", prompt, channel="cli")

        # -- Send prompt and stream output -----------------------------------
        chunks: list[str] = []

        if args.quiet:
            response = await agent.send(prompt)
        else:
            with Live(Markdown("..."), console=console, refresh_per_second=8) as live:

                def on_delta(delta: str) -> None:
                    chunks.append(delta)
                    live.update(Markdown("".join(chunks)))

                response = await agent.send(prompt, on_delta=on_delta)

            if not chunks and response:
                console.print(Markdown(response))

        if response:
            memory.record("assistant", response)
            session_store.record("assistant", response, channel="cli")
            if args.quiet:
                console.print(response)
        else:
            console.print("[yellow]No response from agent.[/yellow]")
            exit_code = 1

        # -- Memory post-processing ------------------------------------------
        if not args.skip_memory:
            console.print("\n[dim]Running memory post-processing...[/dim]")
            try:
                result = await memory.force_form()
                status = result.get("status", "unknown")
                if status == "ok":
                    console.print("[dim]Memory updated.[/dim]")
                elif status == "no_turns":
                    console.print("[dim]No turns to process.[/dim]")
                else:
                    console.print(f"[dim]Memory status: {status}[/dim]")
            except Exception:
                logger.warning("[cli] memory formation failed", exc_info=True)
                console.print("[yellow]Memory post-processing failed (non-fatal).[/yellow]")

    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
        exit_code = 130
    except Exception as exc:
        logger.error("[cli] agent error: %s", exc, exc_info=True)
        console.print(f"[red]Error:[/red] {exc}")
        exit_code = 1
    finally:
        await agent.stop()
        console.print("[dim]Done.[/dim]")

    return exit_code


def main() -> None:
    """CLI entry point for ``polyclaw-run``."""
    parser = _build_parser()
    args = parser.parse_args()

    # If no arguments at all, print help and exit.
    if args.prompt is None and args.file is None:
        parser.print_help()
        sys.exit(1)

    try:
        code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        code = 130

    sys.exit(code)


if __name__ == "__main__":
    main()
