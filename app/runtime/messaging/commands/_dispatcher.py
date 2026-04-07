"""Shared slash-command dispatcher."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...agent.agent import Agent
from ...state.infra_config import InfraConfigStore
from ...state.session_store import SessionStore

from . import agent as _agent_cmds
from . import session as _session_cmds
from . import system as _system_cmds

ReplyFn = Callable[[str], Awaitable[None]]


class ChannelContext(Protocol):
    @property
    def conversation_refs_count(self) -> int: ...

    @property
    def connected_channels(self) -> set[str]: ...

    @property
    def conversation_refs(self) -> list[Any]: ...


@dataclass
class CommandContext:
    text: str
    reply: ReplyFn
    channel: str
    channel_ctx: ChannelContext | None = None


# Maps command name -> (module, function_name)
_CMD_TABLE: dict[str, tuple[object, str]] = {}


def _register(module: object, *names: str) -> None:
    for name in names:
        _CMD_TABLE[name] = (module, f"cmd_{name.lstrip('/')}")


def _init_commands() -> None:
    _register(_session_cmds, "/new", "/model", "/models", "/session",
              "/sessions", "/change", "/clear")
    _register(_agent_cmds, "/skills", "/addskill", "/removeskill",
              "/plugins", "/plugin", "/mcp", "/schedules", "/schedule")
    _register(_system_cmds, "/status", "/channels", "/profile", "/config",
              "/preflight", "/phone", "/call", "/lockdown", "/help")


# Sub-command routing: /sessions clear -> cmd_sessions_sub, /session delete -> cmd_session_sub
_SUB_DISPATCH: dict[str, tuple[object, str]] = {
    "/sessions": (_session_cmds, "cmd_sessions_sub"),
    "/session": (_session_cmds, "cmd_session_sub"),
}


class CommandDispatcher:

    def __init__(
        self,
        agent: Agent,
        session_store: SessionStore | None = None,
        infra: InfraConfigStore | None = None,
    ) -> None:
        self._agent = agent
        self._session_store = session_store
        self._infra = infra
        if not _CMD_TABLE:
            _init_commands()

    @property
    def infra(self) -> InfraConfigStore:
        if self._infra is None:
            self._infra = InfraConfigStore()
        return self._infra

    async def try_handle(
        self,
        text: str,
        reply: ReplyFn,
        channel: str = "web",
        *,
        channel_ctx: ChannelContext | None = None,
    ) -> bool:
        lower = text.lower()
        ctx = CommandContext(text=text, reply=reply, channel=channel, channel_ctx=channel_ctx)

        # Check for sub-commands first (e.g. "/sessions clear")
        for prefix, (mod, fn_name) in _SUB_DISPATCH.items():
            if lower.startswith(prefix + " "):
                parts = lower.split(None, 2)
                if len(parts) >= 2:
                    await getattr(mod, fn_name)(self, ctx)
                    return True

        # Exact match
        entry = _CMD_TABLE.get(lower)
        if entry:
            mod, fn_name = entry
            await getattr(mod, fn_name)(self, ctx)
            return True

        # Prefix match (e.g. "/model gpt-4o" matches "/model")
        for prefix, (mod, fn_name) in _CMD_TABLE.items():
            if lower.startswith(prefix + " "):
                await getattr(mod, fn_name)(self, ctx)
                return True

        return False
