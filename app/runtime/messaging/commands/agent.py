"""Agent-related commands -- skills, plugins, MCP, schedules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...registries.plugins import get_plugin_registry
from ...registries.skills import get_registry as get_skill_registry
from ...scheduler import get_scheduler
from ...state.mcp_config import McpConfigStore

if TYPE_CHECKING:
    from ._dispatcher import CommandContext, CommandDispatcher


async def cmd_skills(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    from ...config.settings import cfg

    skills: list[str] = []
    if cfg.user_skills_dir.is_dir():
        for d in sorted(cfg.user_skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                skills.append(d.name)
    lines = [f"Skills ({len(skills)}):"] + [f"  - {name}" for name in skills]
    if not skills:
        lines.append("  (none)")
    await ctx.reply("\n".join(lines))


async def cmd_addskill(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split(maxsplit=1)
    if len(parts) < 2:
        reg = get_skill_registry()
        try:
            catalog = await reg.fetch_catalog()
            available = [s for s in catalog if not s.installed]
            if available:
                lines = [f"Available skills ({len(available)}):"]
                for s in available:
                    desc = f" - {s.description}" if s.description else ""
                    lines.append(f"  {s.name}{desc}  [{s.source}]")
                lines.append("\nUsage: /addskill <name>")
            else:
                lines = ["All catalog skills already installed.", "Usage: /addskill <name>"]
        except Exception as exc:
            lines = [f"Failed to fetch catalog: {exc}", "Usage: /addskill <name>"]
        await ctx.reply("\n".join(lines))
        return
    name = parts[1].strip()
    reg = get_skill_registry()
    await ctx.reply(f"Installing skill '{name}'...")
    ok = await reg.install(name)
    await ctx.reply(f"Skill '{name}' installed." if ok else f"Failed to install skill '{name}'.")


async def cmd_removeskill(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split(maxsplit=1)
    if len(parts) < 2:
        reg = get_skill_registry()
        installed = reg.list_installed()
        if installed:
            lines = [f"Installed skills ({len(installed)}):"] + [f"  {s.name}" for s in installed]
            lines.append("\nUsage: /removeskill <name>")
        else:
            lines = ["No skills installed.", "Usage: /removeskill <name>"]
        await ctx.reply("\n".join(lines))
        return
    name = parts[1].strip()
    reg = get_skill_registry()
    removed = reg.remove(name)
    await ctx.reply(f"Skill '{name}' removed." if removed else f"Skill '{name}' not found.")


async def cmd_plugins(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    reg = get_plugin_registry()
    plugins = reg.list_plugins()
    if not plugins:
        await ctx.reply("No plugins found.")
        return
    lines = [f"Plugins ({len(plugins)}):"]
    for p in plugins:
        icon = "+" if p.get("enabled") else "-"
        desc = f" - {p['description']}" if p.get("description") else ""
        lines.append(f"  [{icon}] {p['id']}{desc} ({p.get('skill_count', 0)} skills)")
    lines.append("\nUsage: /plugin enable <id>, /plugin disable <id>")
    await ctx.reply("\n".join(lines))


async def cmd_plugin(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split()
    if len(parts) < 3:
        await ctx.reply("Usage: /plugin enable <id> or /plugin disable <id>")
        return
    action, plugin_id = parts[1].lower(), parts[2].strip()
    reg = get_plugin_registry()
    if action == "enable":
        result = reg.enable_plugin(plugin_id)
        await ctx.reply(f"Plugin '{plugin_id}' enabled." if result else f"Plugin '{plugin_id}' not found.")
    elif action == "disable":
        result = reg.disable_plugin(plugin_id)
        await ctx.reply(f"Plugin '{plugin_id}' disabled." if result else f"Plugin '{plugin_id}' not found.")
    else:
        await ctx.reply(f"Unknown action '{action}'.")


async def cmd_mcp(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split()
    store = McpConfigStore()
    if len(parts) == 1:
        servers = store.list_servers()
        if not servers:
            await ctx.reply("No MCP servers configured.")
            return
        lines = [f"MCP Servers ({len(servers)}):"]
        for s in servers:
            icon = "+" if s.get("enabled") else "-"
            builtin = " [builtin]" if s.get("builtin") else ""
            lines.append(f"  [{icon}] {s['name']} ({s.get('type', '?')}){builtin}")
            if s.get("description"):
                lines.append(f"        {s['description']}")
        await ctx.reply("\n".join(lines))
        return

    action = parts[1].lower()
    if action == "add":
        if len(parts) < 4:
            await ctx.reply("Usage: /mcp add <name> <url>")
            return
        try:
            store.add_server(parts[2], "http", url=parts[3])
            await ctx.reply(f"MCP server '{parts[2]}' added. Start a /new session to activate.")
        except ValueError as exc:
            await ctx.reply(f"Error: {exc}")
    elif action == "remove":
        if len(parts) < 3:
            await ctx.reply("Usage: /mcp remove <name>")
            return
        try:
            ok = store.remove_server(parts[2])
            await ctx.reply(f"MCP server '{parts[2]}' removed." if ok else f"MCP server '{parts[2]}' not found.")
        except ValueError as exc:
            await ctx.reply(f"Error: {exc}")
    elif action in ("enable", "disable"):
        if len(parts) < 3:
            await ctx.reply(f"Usage: /mcp {action} <name>")
            return
        ok = store.set_enabled(parts[2], action == "enable")
        await ctx.reply(f"MCP server '{parts[2]}' {action}d." if ok else f"MCP server '{parts[2]}' not found.")
    else:
        await ctx.reply(f"Unknown MCP action '{action}'.")


async def cmd_schedules(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    sched = get_scheduler()
    tasks = sched.list_tasks()
    if not tasks:
        await ctx.reply("No scheduled tasks.\n\nUsage: /schedule add <cron> <prompt>")
        return
    lines = [f"Scheduled Tasks ({len(tasks)}):"]
    for t in tasks:
        icon = "+" if t.enabled else "-"
        schedule = t.cron or (f"once at {t.run_at}" if t.run_at else "?")
        lines.append(f"  [{icon}] {t.id} - {t.description}")
        lines.append(f"        Schedule: {schedule}  |  Last run: {t.last_run[:16] if t.last_run else 'never'}")
    await ctx.reply("\n".join(lines))


async def cmd_schedule(dispatcher: CommandDispatcher, ctx: CommandContext) -> None:
    parts = ctx.text.split()
    if len(parts) < 2:
        await ctx.reply("Usage: /schedule add <cron> <prompt> or /schedule remove <id>")
        return
    action = parts[1].lower()
    sched = get_scheduler()
    if action == "add":
        if len(parts) < 8:
            await ctx.reply("Usage: /schedule add <min> <hour> <dom> <month> <dow> <prompt>")
            return
        cron = " ".join(parts[2:7])
        prompt = " ".join(parts[7:])
        try:
            task = sched.add(description=prompt[:60], prompt=prompt, cron=cron)
            await ctx.reply(f"Scheduled task created:\n  ID: {task.id}\n  Cron: {cron}\n  Prompt: {prompt}")
        except ValueError as exc:
            await ctx.reply(f"Error: {exc}")
    elif action == "remove":
        if len(parts) < 3:
            await ctx.reply("Usage: /schedule remove <id>")
            return
        ok = sched.remove(parts[2])
        await ctx.reply(f"Task '{parts[2]}' removed." if ok else f"Task '{parts[2]}' not found.")
