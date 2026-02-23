"""System, status, and infrastructure commands."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ...config.settings import cfg
from ...scheduler import get_scheduler
from ...state.profile import load_profile

if TYPE_CHECKING:
    from ._dispatcher import CommandContext, CommandDispatcher

BOOT_TIME = time.monotonic()


async def cmd_status(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    uptime_seconds = int(time.monotonic() - BOOT_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    sched = get_scheduler()
    tasks = sched.list_tasks()
    active_tasks = [t for t in tasks if t.enabled]
    total_reqs = sum(dispatcher._agent.request_counts.values())

    lines = [
        "System Status",
        f"  Model: {cfg.copilot_model}",
        f"  Uptime: {hours}h {minutes}m {seconds}s",
        f"  Total requests: {total_reqs}",
    ]
    for model, count in sorted(dispatcher._agent.request_counts.items()):
        lines.append(f"    {model}: {count}")
    if ctx.channel_ctx is not None:
        channels = ctx.channel_ctx.connected_channels
        lines.append(f"  Connected channels: {', '.join(sorted(channels)) or 'none'}")
        lines.append(f"  Conversation refs: {ctx.channel_ctx.conversation_refs_count}")
    lines.append(f"  Scheduled tasks: {len(active_tasks)} active / {len(tasks)} total")
    lines.append(f"  Data dir: {cfg.data_dir}")
    await ctx.reply("\n".join(lines))


async def cmd_channels(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    lines = ["Channel Configuration\n"]
    tg = dispatcher.infra.channels.telegram
    if tg.token:
        masked = tg.token[:8] + "..." + tg.token[-4:] if len(tg.token) > 12 else "***"
        lines.append(f"Telegram:\n  Token: {masked}\n  Whitelist: {tg.whitelist or '(none)'}")
    else:
        lines.append("Telegram: not configured")
    lines.append(f"\nBot Framework:\n  App ID: {cfg.bot_app_id[:8] + '...' if cfg.bot_app_id else 'not set'}")
    lines.append(f"  Tenant: {cfg.bot_app_tenant_id[:8] + '...' if cfg.bot_app_tenant_id else 'not set'}")
    lines.append(f"  Admin secret: {'set' if cfg.admin_secret else 'not set'}")
    if ctx.channel_ctx is not None:
        refs = ctx.channel_ctx.conversation_refs
        lines.append(f"\nActive Conversations ({len(refs)}):")
        for r in refs:
            user_name = r.user.name if r.user else "?"
            lines.append(f"  - {r.channel_id}: {user_name}")
    await ctx.reply("\n".join(lines))


async def cmd_profile(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    profile = load_profile()
    lines = [
        "Agent Profile",
        f"  Name: {profile.get('name') or '(not set)'}",
        f"  Location: {profile.get('location') or '(not set)'}",
        f"  Emotional state: {profile.get('emotional_state', 'neutral')}",
    ]
    prefs = profile.get("preferences", {})
    if prefs:
        lines.append("  Preferences:")
        for k, v in prefs.items():
            lines.append(f"    {k}: {v}")
    await ctx.reply("\n".join(lines))


async def cmd_config(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split(maxsplit=2)
    if len(parts) == 1:
        lines = [
            "Runtime Configuration",
            f"  Model: {cfg.copilot_model}",
            f"  Admin port: {cfg.admin_port}",
            f"  Bot port: {cfg.bot_port}",
            f"  Data dir: {cfg.data_dir}",
            f"  Admin secret: {'set' if cfg.admin_secret else 'not set'}",
            "\nUsage: /config <KEY> <VALUE>",
        ]
        await ctx.reply("\n".join(lines))
        return
    if len(parts) < 3:
        await ctx.reply("Usage: /config <KEY> <VALUE>")
        return
    key = parts[1].upper()
    allowed = {"COPILOT_MODEL", "ADMIN_PORT", "BOT_PORT", "VOICE_TARGET_NUMBER", "ACS_SOURCE_NUMBER"}
    if key not in allowed:
        await ctx.reply(f"Cannot set '{key}'. Allowed keys: {', '.join(sorted(allowed))}")
        return
    cfg.write_env(**{key: parts[2]})
    await ctx.reply(f"Config updated: {key} = {parts[2]}")


async def cmd_preflight(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    import aiohttp as _aiohttp

    base = f"http://127.0.0.1:{cfg.admin_port}"
    headers = {"Authorization": f"Bearer {cfg.admin_secret}"} if cfg.admin_secret else {}
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.get(f"{base}/api/setup/preflight", headers=headers, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    await ctx.reply(f"Preflight check failed (HTTP {resp.status}).")
                    return
                data = await resp.json()
    except Exception as exc:
        await ctx.reply(f"Cannot reach preflight endpoint: {exc}")
        return

    checks = data.get("checks", [])
    lines = [f"Preflight Checks ({data.get('status', '?').upper()})"]
    for c in checks:
        icon = "OK" if c.get("ok") else "!!"
        lines.append(f"  [{icon}] {c['check']}: {c.get('detail', '')}")
    await ctx.reply("\n".join(lines))


async def cmd_phone(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split(maxsplit=1)
    if len(parts) < 2:
        await ctx.reply(f"Current target number: {cfg.voice_target_number or '(not set)'}\n\nUsage: /phone <number>")
        return
    number = parts[1].strip()
    if not number.startswith("+"):
        await ctx.reply("Phone number must start with + country code.")
        return
    cfg.write_env(VOICE_TARGET_NUMBER=number)
    await ctx.reply(f"Voice target number set to {number}.")


async def cmd_call(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    import aiohttp as _aiohttp

    target = cfg.voice_target_number
    if not target:
        await ctx.reply("No target number configured. Use /phone <number> first.")
        return
    base = f"http://127.0.0.1:{cfg.admin_port}"
    headers = {"Authorization": f"Bearer {cfg.admin_secret}"} if cfg.admin_secret else {}
    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(f"{base}/api/voice/call", json={"target_number": target}, headers=headers, timeout=_aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                if resp.status == 200:
                    await ctx.reply(f"Calling {target}...")
                else:
                    await ctx.reply(f"Call failed: {data.get('error', f'HTTP {resp.status}')}")
    except Exception as exc:
        await ctx.reply(f"Call failed: {exc}")


async def cmd_lockdown(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split()
    if len(parts) < 2:
        state = "ENABLED" if cfg.lockdown_mode else "disabled"
        await ctx.reply(f"Lock Down Mode: {state}\n\nUsage: /lockdown on | /lockdown off")
        return
    action = parts[1].lower()
    if action not in ("on", "off"):
        await ctx.reply("Usage: /lockdown on | /lockdown off")
        return
    if action == "on":
        if cfg.lockdown_mode:
            await ctx.reply("Lock Down Mode is already enabled.")
            return
        cfg.write_env(LOCKDOWN_MODE="1", TUNNEL_RESTRICTED="1")
        from ...services.cloud.azure import AzureCLI
        az = AzureCLI()
        az.ok("logout")
        az.invalidate_cache("account", "show")
        await ctx.reply("Lock Down Mode ENABLED\n\n  - Azure CLI logged out\n  - Admin panel disabled")
    else:
        if not cfg.lockdown_mode:
            await ctx.reply("Lock Down Mode is already disabled.")
            return
        cfg.write_env(LOCKDOWN_MODE="", TUNNEL_RESTRICTED="")
        await ctx.reply("Lock Down Mode DISABLED\n\n  - Admin panel re-enabled")


async def cmd_help(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    lines = [
        "Available Commands",
        "",
        "  /new, /model <name>, /models, /status, /session, /config",
        "  /skills, /addskill <name>, /removeskill <name>",
        "  /plugins, /plugin enable|disable <id>",
        "  /mcp, /mcp add|remove|enable|disable <name>",
        "  /schedules, /schedule add|remove",
        "  /sessions, /session delete <id>, /sessions clear",
        "  /change, /profile, /channels, /clear",
        "  /phone <number>, /call, /preflight, /lockdown, /help",
    ]
    await ctx.reply("\n".join(lines))
