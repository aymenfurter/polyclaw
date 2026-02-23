"""Agent sandbox executor -- runs agent commands in ACA Dynamic Sessions.

.. warning:: This feature is experimental and may change or be removed in
   future releases.
"""

from __future__ import annotations

from .executor import SandboxExecutor
from .helpers import _build_replay_command, _extract_command, _is_shell_tool, _parse_tool_args
from .interceptor import SandboxToolInterceptor

__all__ = [
    "SandboxExecutor",
    "SandboxToolInterceptor",
    "_build_replay_command",
    "_extract_command",
    "_is_shell_tool",
    "_parse_tool_args",
]
