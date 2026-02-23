"""Session and model management commands."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from ...config.settings import cfg

if TYPE_CHECKING:
    from ._dispatcher import CommandContext, CommandDispatcher


async def cmd_new(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    await dispatcher._agent.new_session()
    if dispatcher._session_store:
        dispatcher._session_store.start_session(uuid.uuid4().hex[:12], model=cfg.copilot_model)
    await ctx.reply("New session started.")


async def cmd_model(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split(maxsplit=1)
    if len(parts) < 2:
        await ctx.reply(f"Current model: {cfg.copilot_model}\n\nUsage: /model <name>")
        return
    new_model = parts[1].strip()
    old_model = cfg.copilot_model
    cfg.write_env(COPILOT_MODEL=new_model)
    await dispatcher._agent.new_session()
    if dispatcher._session_store:
        dispatcher._session_store.start_session(uuid.uuid4().hex[:12], model=new_model)
    await ctx.reply(f"Model switched: {old_model} -> {new_model}\nNew session started.")


async def cmd_models(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    models = await dispatcher._agent.list_models()
    if not models:
        await ctx.reply("No models available.")
        return
    current = cfg.copilot_model
    lines = ["Available Models", ""]
    for m in models:
        marker = " *" if m["id"] == current else ""
        cost = f" ({m['billing_multiplier']}x)" if m.get("billing_multiplier", 1.0) != 1.0 else ""
        reasoning = f"  [reasoning: {', '.join(m['reasoning_efforts'])}]" if m.get("reasoning_efforts") else ""
        policy = m.get("policy", "enabled")
        if policy != "enabled":
            lines.append(f"  {m['id']}{marker}{cost}  ({policy})")
        else:
            lines.append(f"  {m['id']}{marker}{cost}{reasoning}")
    lines.append(f"\nCurrent: {current}\nUse /model <name> to switch.")
    await ctx.reply("\n".join(lines))


async def cmd_session(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    lines = [
        "Session Info",
        f"  Active: {'yes' if dispatcher._agent.has_session else 'no'}",
        f"  Model: {cfg.copilot_model}",
        "  Playwright MCP: enabled",
    ]
    await ctx.reply("\n".join(lines))


async def cmd_sessions(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    if not dispatcher._session_store:
        await ctx.reply("Session store not available.")
        return
    sessions = dispatcher._session_store.list_sessions()
    if not sessions:
        await ctx.reply("No recorded sessions.")
        return
    stats = dispatcher._session_store.get_session_stats()
    lines = [f"Sessions ({stats['total_sessions']} total, {stats['total_messages']} messages)", ""]
    for s in sessions[:10]:
        started = s.get("started_at", "?")[:16]
        lines.append(f"  {s['id']}  {started}  {s.get('model', '?')}  ({s.get('message_count', 0)} msgs)")
    if len(sessions) > 10:
        lines.append(f"  ... and {len(sessions) - 10} more")
    await ctx.reply("\n".join(lines))


async def cmd_sessions_sub(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split()
    if len(parts) >= 2 and parts[1].lower() == "clear":
        if not dispatcher._session_store:
            await ctx.reply("Session store not available.")
            return
        count = dispatcher._session_store.clear_all()
        await ctx.reply(f"All sessions cleared ({count} deleted).")
    else:
        await cmd_sessions(dispatcher, ctx)


async def cmd_session_sub(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split()
    if len(parts) >= 3 and parts[1].lower() == "delete":
        if not dispatcher._session_store:
            await ctx.reply("Session store not available.")
            return
        ok = dispatcher._session_store.delete_session(parts[2])
        await ctx.reply(f"Session '{parts[2]}' deleted." if ok else f"Session '{parts[2]}' not found.")
    else:
        await cmd_session(dispatcher, ctx)


async def cmd_change(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    if not dispatcher._session_store:
        await ctx.reply("Session store not available.")
        return
    sessions = dispatcher._session_store.list_sessions()
    if not sessions:
        await ctx.reply("No sessions to switch to. Use /new to start one.")
        return
    lines = ["Recent Sessions:", ""]
    for i, s in enumerate(sessions[:5], 1):
        started = s.get("started_at", "?")[:16]
        lines.append(f"  {i}. {started}  {s.get('model', '?')}  ({s.get('message_count', 0)} msgs)")
        lines.append(f"     ID: {s['id']}")
    await ctx.reply("\n".join(lines))


async def cmd_clear(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    cleared = 0
    if cfg.memory_dir.is_dir():
        for f in cfg.memory_dir.rglob("*"):
            if f.is_file():
                f.unlink()
                cleared += 1
    await ctx.reply(f"Memory cleared ({cleared} files removed).")
