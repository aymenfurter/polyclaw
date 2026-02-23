"""Slash-command dispatcher and command implementations.

Sub-modules group commands by domain:

- ``agent``   -- skills, plugins, MCP, schedules
- ``session`` -- session lifecycle and model switching
- ``system``  -- status, infra, and connectivity commands
"""

from ._dispatcher import (
    ChannelContext,
    CommandContext,
    CommandDispatcher,
    ReplyFn,
)
from .system import BOOT_TIME

__all__ = [
    "BOOT_TIME",
    "ChannelContext",
    "CommandContext",
    "CommandDispatcher",
    "ReplyFn",
]
