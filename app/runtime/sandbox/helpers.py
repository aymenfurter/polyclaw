"""Sandbox helper utilities for tool argument parsing and command replay."""

from __future__ import annotations

import json
import shlex
from typing import Any

_SHELL_TOOL_PATTERNS = ("terminal", "shell", "bash", "command")


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
