"""TTY-based tool approval for CLI mode.

Provides a terminal-based approval callback that prints a prompt and
reads a single ``y``/``n`` keypress when guardrails require human
confirmation.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from rich.console import Console

logger = logging.getLogger(__name__)

_console = Console(stderr=True)


async def tty_approve(
    input_data: dict[str, Any],
    invocation: Any,
    *,
    hitl_interceptor: Any | None = None,
) -> dict[str, str]:
    """Pre-tool-use hook that prompts the user in the terminal.

    When a ``HitlInterceptor`` is provided this function is used as the
    emitter: it prints the approval request, reads input, and resolves the
    pending future so the interceptor's regular flow proceeds.

    When no interceptor is provided (standalone mode) this function acts
    as a direct ``on_pre_tool_use`` hook.
    """
    tool_name = input_data.get("toolName", "unknown")
    args_str = str(input_data.get("toolArgs") or input_data.get("input", ""))
    if len(args_str) > 300:
        args_str = args_str[:297] + "..."

    _console.print(
        f"\n[bold yellow]Tool approval required:[/bold yellow] [bold]{tool_name}[/bold]"
    )
    if args_str:
        _console.print(f"[dim]Arguments: {args_str}[/dim]")
    _console.print("[bold]Allow? [y/n][/bold] ", end="")

    approved = await asyncio.to_thread(_read_yn)

    decision = "allow" if approved else "deny"
    label = "[green]approved[/green]" if approved else "[red]denied[/red]"
    _console.print(label)
    logger.info("[cli.approve] tool=%s decision=%s", tool_name, decision)
    return {"permissionDecision": decision}


def _read_yn() -> bool:
    """Read a single y/n answer from stdin.

    If stdin is not a TTY (piped input), defaults to deny for safety.
    """
    if not sys.stdin.isatty():
        logger.info("[cli.approve] stdin is not a TTY, defaulting to deny")
        return False

    try:
        response = input().strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False
