"""Shared slash-command dispatcher.

Centralises all slash-command logic so both the Bot Framework handler
and the WebSocket chat handler share a single implementation.
"""

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


class CommandDispatcher:
    _EXACT_COMMANDS: dict[str, str] = {
        "/new": "_cmd_new",
        "/status": "_cmd_status",
        "/skills": "_cmd_skills",
        "/session": "_cmd_session",
        "/channels": "_cmd_channels",
        "/clear": "_cmd_clear",
        "/help": "_cmd_help",
        "/plugins": "_cmd_plugins",
        "/mcp": "_cmd_mcp",
        "/schedules": "_cmd_schedules",
        "/sessions": "_cmd_sessions",
        "/profile": "_cmd_profile",
        "/config": "_cmd_config",
        "/preflight": "_cmd_preflight",
        "/call": "_cmd_call",
        "/models": "_cmd_models",
        "/change": "_cmd_change",
    }

    _PREFIX_COMMANDS: tuple[tuple[str, str], ...] = (
        ("/removeskill", "_cmd_removeskill"),
        ("/addskill", "_cmd_addskill"),
        ("/model", "_cmd_model"),
        ("/plugin", "_cmd_plugin"),
        ("/mcp", "_cmd_mcp"),
        ("/schedule", "_cmd_schedule"),
        ("/sessions", "_cmd_sessions_sub"),
        ("/session", "_cmd_session_sub"),
        ("/config", "_cmd_config"),
        ("/phone", "_cmd_phone"),
        ("/lockdown", "_cmd_lockdown"),
    )

    def __init__(
        self,
        agent: Agent,
        session_store: SessionStore | None = None,
        infra: InfraConfigStore | None = None,
    ) -> None:
        self._agent = agent
        self._session_store = session_store
        self._infra = infra

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

        handler_name = self._EXACT_COMMANDS.get(lower)
        if handler_name:
            await getattr(self, handler_name)(ctx)
            return True

        for prefix, handler_name in self._PREFIX_COMMANDS:
            if lower.startswith(prefix):
                await getattr(self, handler_name)(ctx)
                return True

        return False

    # -- Session & model commands (delegated to commands_session) -----------

    async def _cmd_new(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_new(self, ctx)

    async def _cmd_model(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_model(self, ctx)

    async def _cmd_models(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_models(self, ctx)

    async def _cmd_session(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_session(self, ctx)

    async def _cmd_sessions(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_sessions(self, ctx)

    async def _cmd_sessions_sub(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_sessions_sub(self, ctx)

    async def _cmd_session_sub(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_session_sub(self, ctx)

    async def _cmd_change(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_change(self, ctx)

    async def _cmd_clear(self, ctx: CommandContext) -> None:
        await _session_cmds.cmd_clear(self, ctx)

    # -- Agent commands (delegated to commands_agent) ----------------------

    async def _cmd_skills(self, ctx: CommandContext) -> None:
        await _agent_cmds.cmd_skills(self, ctx)

    async def _cmd_addskill(self, ctx: CommandContext) -> None:
        await _agent_cmds.cmd_addskill(self, ctx)

    async def _cmd_removeskill(self, ctx: CommandContext) -> None:
        await _agent_cmds.cmd_removeskill(self, ctx)

    async def _cmd_plugins(self, ctx: CommandContext) -> None:
        await _agent_cmds.cmd_plugins(self, ctx)

    async def _cmd_plugin(self, ctx: CommandContext) -> None:
        await _agent_cmds.cmd_plugin(self, ctx)

    async def _cmd_mcp(self, ctx: CommandContext) -> None:
        await _agent_cmds.cmd_mcp(self, ctx)

    async def _cmd_schedules(self, ctx: CommandContext) -> None:
        await _agent_cmds.cmd_schedules(self, ctx)

    async def _cmd_schedule(self, ctx: CommandContext) -> None:
        await _agent_cmds.cmd_schedule(self, ctx)

    # -- System commands (delegated to commands_system) --------------------

    async def _cmd_status(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_status(self, ctx)

    async def _cmd_channels(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_channels(self, ctx)

    async def _cmd_profile(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_profile(self, ctx)

    async def _cmd_config(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_config(self, ctx)

    async def _cmd_preflight(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_preflight(self, ctx)

    async def _cmd_phone(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_phone(self, ctx)

    async def _cmd_call(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_call(self, ctx)

    async def _cmd_lockdown(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_lockdown(self, ctx)

    async def _cmd_help(self, ctx: CommandContext) -> None:
        await _system_cmds.cmd_help(self, ctx)
