"""Guardrails metadata handlers -- static context, template, and tool data."""

from __future__ import annotations

from typing import Any

from aiohttp import web

BUILTIN_SDK_TOOLS: list[dict[str, str]] = [
    {"name": "create", "source": "sdk", "description": "Create a new file"},
    {"name": "edit", "source": "sdk", "description": "Edit an existing file"},
    {"name": "view", "source": "sdk", "description": "View file contents"},
    {"name": "grep", "source": "sdk", "description": "Search file contents"},
    {"name": "glob", "source": "sdk", "description": "Find files by pattern"},
    {"name": "run", "source": "sdk", "description": "Run a shell command"},
    {"name": "bash", "source": "sdk", "description": "Run a bash command"},
    {"name": "report_intent", "source": "sdk",
     "description": "Log agent intent (always auto-approved)"},
]


async def list_contexts_handler(_req: web.Request) -> web.Response:
    """Return available execution contexts, HITL channels, and strategies."""
    return web.json_response({
        "status": "ok",
        "contexts": [
            {"id": "interactive", "label": "Interactive",
             "description": "User is chatting via the web UI or TUI"},
            {"id": "background", "label": "Background",
             "description": "Scheduled tasks and proactive loop"},
            {"id": "voice", "label": "Voice",
             "description": "Realtime voice call sessions"},
            {"id": "api", "label": "API",
             "description": "External API-triggered executions"},
        ],
        "channels": [
            {"id": "chat", "label": "Chat",
             "description": "In-session WebSocket approval prompt"},
            {"id": "phone", "label": "Phone Call",
             "description": "Outbound phone call verification via ACS"},
        ],
        "strategies": [
            {"id": "allow", "label": "Allow",
             "description": "Pass through without review", "color": "var(--ok)"},
            {"id": "deny", "label": "Deny",
             "description": "Block immediately", "color": "var(--err)"},
            {"id": "hitl", "label": "HITL",
             "description": "Human-in-the-loop approval via chat",
             "color": "var(--blue)"},
            {"id": "pitl", "label": "PITL (Experimental)",
             "description": "Phone-in-the-loop approval via outbound phone call"
                            " (experimental)",
             "color": "var(--cyan, #22d3ee)"},
            {"id": "aitl", "label": "AITL",
             "description": "AI-in-the-loop: background reviewer agent decides",
             "color": "var(--gold)"},
            {"id": "filter", "label": "Filter",
             "description": "Content Safety Prompt Shields injection detection",
             "color": "var(--purple, #a78bfa)"},
        ],
    })


async def list_templates_handler(_req: web.Request) -> web.Response:
    """Return the list of prompt template names."""
    from pathlib import Path as _Path

    from ...agent.prompt import TEMPLATES_DIR
    from ...config.settings import cfg

    templates: list[dict[str, str]] = []
    if TEMPLATES_DIR.is_dir():
        for f in sorted(TEMPLATES_DIR.iterdir()):
            if f.suffix == ".md":
                templates.append({
                    "name": f.name,
                    "size": str(f.stat().st_size),
                })
    if cfg.soul_path.exists():
        templates.insert(0, {
            "name": "SOUL.md",
            "size": str(cfg.soul_path.stat().st_size),
        })
    return web.json_response({"status": "ok", "templates": templates})


async def get_template_handler(req: web.Request) -> web.Response:
    """Fetch the content of a single prompt template."""
    name = req.match_info["name"]
    if ".." in name or "/" in name:
        return web.json_response(
            {"status": "error", "message": "invalid name"}, status=400,
        )
    if name == "SOUL.md":
        from ...config.settings import cfg

        if cfg.soul_path.exists():
            return web.json_response({
                "status": "ok",
                "name": name,
                "content": cfg.soul_path.read_text(),
            })
        return web.json_response(
            {"status": "error", "message": "not found"}, status=404,
        )
    from ...agent.prompt import TEMPLATES_DIR

    path = TEMPLATES_DIR / name
    if not path.exists() or not path.suffix == ".md":
        return web.json_response(
            {"status": "error", "message": "not found"}, status=404,
        )
    return web.json_response({
        "status": "ok",
        "name": name,
        "content": path.read_text(),
    })


def collect_tools() -> list[dict[str, Any]]:
    """Gather custom tools defined via ``@define_tool`` plus built-in SDK tools."""
    from ...agent.tools import get_all_tools

    result: list[dict[str, Any]] = []
    for t in get_all_tools():
        name = getattr(t, "name", "") or getattr(t, "__name__", "unknown")
        desc = getattr(t, "description", "") or ""
        if not desc and hasattr(t, "__doc__") and t.__doc__:
            first_line = t.__doc__.strip().split("\n")[0]
            if not first_line.startswith("Tool("):
                desc = first_line
        result.append({"name": name, "source": "custom", "description": desc})
    for entry in BUILTIN_SDK_TOOLS:
        result.append(dict(entry))
    return result
