"""Guardrails configuration API routes -- /api/guardrails/*."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from ...agent.tools import get_all_tools
from ...registries.skills import SkillRegistry
from ...state.guardrails_config import (
    GuardrailsConfigStore,
    list_model_tiers,
    list_presets,
)
from ...state.mcp_config import McpConfigStore

logger = logging.getLogger(__name__)

_BUILTIN_SDK_TOOLS: list[dict[str, str]] = [
    {"name": "create", "source": "sdk", "description": "Create a new file"},
    {"name": "edit", "source": "sdk", "description": "Edit an existing file"},
    {"name": "view", "source": "sdk", "description": "View file contents"},
    {"name": "grep", "source": "sdk", "description": "Search file contents"},
    {"name": "glob", "source": "sdk", "description": "Find files by pattern"},
    {"name": "run", "source": "sdk", "description": "Run a shell command"},
    {"name": "bash", "source": "sdk", "description": "Run a bash command"},
    {"name": "report_intent", "source": "sdk", "description": "Log agent intent (always auto-approved)"},
]


class GuardrailsRoutes:
    """REST handler for guardrails HITL configuration."""

    def __init__(
        self,
        guardrails_store: GuardrailsConfigStore,
        mcp_store: McpConfigStore,
        skills_registry: SkillRegistry | None = None,
    ) -> None:
        self._store = guardrails_store
        self._mcp = mcp_store
        self._skills = skills_registry

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/guardrails/config", self._get_config)
        router.add_put("/api/guardrails/config", self._update_config)
        router.add_get("/api/guardrails/rules", self._list_rules)
        router.add_post("/api/guardrails/rules", self._add_rule)
        router.add_put("/api/guardrails/rules/bulk", self._bulk_rules)
        router.add_put("/api/guardrails/rules/{rule_id}", self._update_rule)
        router.add_delete("/api/guardrails/rules/{rule_id}", self._delete_rule)
        router.add_get("/api/guardrails/tools", self._list_tools)
        router.add_get("/api/guardrails/inventory", self._list_inventory)
        router.add_put("/api/guardrails/policies/{ctx}/{tool_id}", self._set_policy)
        router.add_post("/api/guardrails/model-columns", self._add_model_column)
        router.add_delete("/api/guardrails/model-columns/{model}", self._remove_model_column)
        router.add_put("/api/guardrails/model-policies/{model}/{ctx}/{tool_id}", self._set_model_policy)
        router.add_get("/api/guardrails/contexts", self._list_contexts)
        router.add_get("/api/guardrails/presets", self._list_presets)
        router.add_post("/api/guardrails/presets/{preset_id}", self._apply_preset)
        router.add_post("/api/guardrails/set-all", self._set_all)
        router.add_post("/api/guardrails/model-defaults", self._apply_model_defaults)
        router.add_get("/api/guardrails/model-tiers", self._list_model_tiers)
        router.add_get("/api/guardrails/templates", self._list_templates)
        router.add_get("/api/guardrails/templates/{name}", self._get_template)
        router.add_get("/api/guardrails/background-agents", self._list_background_agents)
        router.add_get("/api/guardrails/policy-yaml", self._get_policy_yaml)
        router.add_put("/api/guardrails/policy-yaml", self._put_policy_yaml)

    async def _get_config(self, _req: web.Request) -> web.Response:
        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def _update_config(self, req: web.Request) -> web.Response:
        data = await req.json()
        # Accept both frontend ('enabled') and backend ('hitl_enabled') field names.
        if "enabled" in data:
            self._store.set_hitl_enabled(bool(data["enabled"]))
        if "hitl_enabled" in data:
            self._store.set_hitl_enabled(bool(data["hitl_enabled"]))
        # Accept both 'default_strategy' (frontend) and 'default_action' (backend).
        if "default_strategy" in data:
            try:
                self._store.set_default_action(data["default_strategy"])
            except ValueError as exc:
                return web.json_response(
                    {"status": "error", "message": str(exc)}, status=400
                )
        if "default_action" in data:
            try:
                self._store.set_default_action(data["default_action"])
            except ValueError as exc:
                return web.json_response(
                    {"status": "error", "message": str(exc)}, status=400
                )
        # Accept both 'hitl_channel' (frontend) and 'default_channel' (backend).
        if "hitl_channel" in data:
            try:
                self._store.set_default_channel(data["hitl_channel"])
            except ValueError as exc:
                return web.json_response(
                    {"status": "error", "message": str(exc)}, status=400
                )
        if "default_channel" in data:
            try:
                self._store.set_default_channel(data["default_channel"])
            except ValueError as exc:
                return web.json_response(
                    {"status": "error", "message": str(exc)}, status=400
                )
        if "phone_number" in data:
            self._store.set_phone_number(data["phone_number"])
        if "aitl_model" in data:
            self._store.set_aitl_model(data["aitl_model"])
        if "aitl_spotlighting" in data:
            self._store.set_aitl_spotlighting(bool(data["aitl_spotlighting"]))
        if "filter_mode" in data:
            try:
                self._store.set_filter_mode(data["filter_mode"])
            except ValueError as exc:
                return web.json_response(
                    {"status": "error", "message": str(exc)}, status=400
                )
        if "content_safety_endpoint" in data:
            self._store.set_content_safety_endpoint(data["content_safety_endpoint"])
        if "context_defaults" in data:
            for ctx, strategy in data["context_defaults"].items():
                try:
                    self._store.set_context_default(ctx, strategy)
                except ValueError as exc:
                    return web.json_response(
                        {"status": "error", "message": str(exc)}, status=400
                    )
        # Single context_default update (used by Background Agents tab)
        if "context_default" in data:
            cd = data["context_default"]
            ctx = cd.get("context", "")
            strategy = cd.get("strategy", "")
            if ctx:
                if strategy:
                    try:
                        self._store.set_context_default(ctx, strategy)
                    except ValueError as exc:
                        return web.json_response(
                            {"status": "error", "message": str(exc)}, status=400
                        )
                else:
                    self._store.remove_context_default(ctx)
        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def _list_rules(self, _req: web.Request) -> web.Response:
        from dataclasses import asdict

        rules = [asdict(r) for r in self._store.rules]
        return web.json_response({"status": "ok", "rules": rules})

    async def _add_rule(self, req: web.Request) -> web.Response:
        data = await req.json()
        try:
            rule = self._store.add_rule(
                name=data.get("name", ""),
                pattern=data.get("pattern", ""),
                scope=data.get("scope", "tool"),
                action=data.get("action", "ask"),
                enabled=data.get("enabled", True),
                description=data.get("description", ""),
                contexts=data.get("contexts") or [],
                models=data.get("models") or [],
                hitl_channel=data.get("hitl_channel", "chat"),
            )
        except ValueError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        from dataclasses import asdict

        return web.json_response({"status": "ok", "rule": asdict(rule)})

    async def _update_rule(self, req: web.Request) -> web.Response:
        rule_id = req.match_info["rule_id"]
        data = await req.json()
        rule = self._store.update_rule(rule_id, **data)
        if not rule:
            return web.json_response(
                {"status": "error", "message": "Rule not found"}, status=404
            )
        from dataclasses import asdict

        return web.json_response({"status": "ok", "rule": asdict(rule)})

    async def _bulk_rules(self, req: web.Request) -> web.Response:
        """Replace all rules at once (used for template application)."""
        data = await req.json()
        rules_data = data.get("rules", [])
        default_action = data.get("default_action")
        default_channel = data.get("default_channel")
        hitl_enabled = data.get("hitl_enabled")
        phone_number = data.get("phone_number")

        # Clear existing rules
        for r in list(self._store.rules):
            self._store.remove_rule(r.id)

        if hitl_enabled is not None:
            self._store.set_hitl_enabled(bool(hitl_enabled))
        if default_action:
            try:
                self._store.set_default_action(default_action)
            except ValueError as exc:
                return web.json_response(
                    {"status": "error", "message": str(exc)}, status=400
                )
        if default_channel:
            try:
                self._store.set_default_channel(default_channel)
            except ValueError as exc:
                return web.json_response(
                    {"status": "error", "message": str(exc)}, status=400
                )
        if phone_number is not None:
            self._store.set_phone_number(phone_number)

        # Add new rules
        for rd in rules_data:
            try:
                self._store.add_rule(
                    name=rd.get("name", ""),
                    pattern=rd.get("pattern", ""),
                    scope=rd.get("scope", "tool"),
                    action=rd.get("action", "ask"),
                    enabled=rd.get("enabled", True),
                    description=rd.get("description", ""),
                    contexts=rd.get("contexts") or [],
                    models=rd.get("models") or [],
                    hitl_channel=rd.get("hitl_channel", "chat"),
                )
            except ValueError:
                continue

        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def _delete_rule(self, req: web.Request) -> web.Response:
        rule_id = req.match_info["rule_id"]
        if not self._store.remove_rule(rule_id):
            return web.json_response(
                {"status": "error", "message": "Rule not found"}, status=404
            )
        return web.json_response({"status": "ok"})

    async def _list_tools(self, _req: web.Request) -> web.Response:
        """Return all tools and MCP servers available to the agent."""
        tools = self._collect_tools()
        mcps = self._collect_mcps()
        return web.json_response({
            "status": "ok",
            "tools": tools,
            "mcp_servers": mcps,
        })

    async def _list_inventory(self, _req: web.Request) -> web.Response:
        """Return a unified tool inventory for the policy matrix UI."""
        inventory: list[dict[str, Any]] = []
        for t in self._collect_tools():
            inventory.append({
                "id": t["name"],
                "name": t["name"],
                "category": t.get("source", "custom"),
                "source": t.get("source", "custom"),
                "description": t.get("description", ""),
            })
        for m in self._collect_mcps():
            inventory.append({
                "id": f"mcp:{m['name']}",
                "name": m["name"],
                "category": "mcp",
                "source": "mcp",
                "description": m.get("description", ""),
                "enabled": m.get("enabled", True),
                "server_type": m.get("type", ""),
                "builtin": m.get("builtin", False),
            })
        for s in self._collect_skills():
            inventory.append({
                "id": f"skill:{s['name']}",
                "name": s["name"],
                "category": "skill",
                "source": "skill",
                "description": s.get("description", ""),
            })
        return web.json_response({"status": "ok", "inventory": inventory})

    async def _set_policy(self, req: web.Request) -> web.Response:
        """Set a per-tool strategy for a given execution context."""
        ctx = req.match_info["ctx"]
        tool_id = req.match_info["tool_id"]
        data = await req.json()
        strategy = data.get("strategy", "allow")
        try:
            self._store.set_tool_policy(ctx, tool_id, strategy)
        except ValueError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        return web.json_response({"status": "ok"})

    async def _add_model_column(self, req: web.Request) -> web.Response:
        """Add a custom model column to the policy matrix."""
        data = await req.json()
        model = data.get("model", "").strip()
        if not model:
            return web.json_response(
                {"status": "error", "message": "model is required"}, status=400
            )
        self._store.add_model_column(model)
        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def _remove_model_column(self, req: web.Request) -> web.Response:
        """Remove a custom model column from the policy matrix."""
        model = req.match_info["model"]
        if not self._store.remove_model_column(model):
            return web.json_response(
                {"status": "error", "message": "Model column not found"}, status=404
            )
        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def _set_model_policy(self, req: web.Request) -> web.Response:
        """Set a per-tool strategy for a specific model column and context."""
        model = req.match_info["model"]
        ctx = req.match_info["ctx"]
        tool_id = req.match_info["tool_id"]
        data = await req.json()
        strategy = data.get("strategy", "allow")
        try:
            self._store.set_model_policy(model, tool_id, strategy, context=ctx)
        except ValueError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        return web.json_response({"status": "ok"})

    def _collect_tools(self) -> list[dict[str, Any]]:
        """Gather custom tools defined via @define_tool + built-in SDK tools."""
        result: list[dict[str, Any]] = []
        for t in get_all_tools():
            name = getattr(t, "name", "") or getattr(t, "__name__", "unknown")
            desc = getattr(t, "description", "") or ""
            # Avoid using the class-level __doc__ which is the Tool repr
            if not desc and hasattr(t, "__doc__") and t.__doc__:
                first_line = t.__doc__.strip().split("\n")[0]
                if not first_line.startswith("Tool("):
                    desc = first_line
            result.append({"name": name, "source": "custom", "description": desc})
        for entry in _BUILTIN_SDK_TOOLS:
            result.append(dict(entry))
        return result

    def _collect_mcps(self) -> list[dict[str, Any]]:
        """Gather configured MCP servers."""
        return [
            {
                "name": srv["name"],
                "enabled": srv.get("enabled", True),
                "description": srv.get("description", ""),
                "type": srv.get("type", ""),
                "builtin": srv.get("builtin", False),
            }
            for srv in self._mcp.list_servers()
        ]

    def _collect_skills(self) -> list[dict[str, Any]]:
        """Gather installed skills."""
        if not self._skills:
            return []
        return [
            {
                "name": s.name,
                "description": s.description or s.verb or "",
            }
            for s in self._skills.list_installed()
        ]

    async def _list_contexts(self, _req: web.Request) -> web.Response:
        """Return available execution contexts, HITL channels, and strategies."""
        return web.json_response({
            "status": "ok",
            "contexts": [
                {"id": "interactive", "label": "Interactive", "description": "User is chatting via the web UI or TUI"},
                {"id": "background", "label": "Background", "description": "Scheduled tasks and proactive loop"},
                {"id": "voice", "label": "Voice", "description": "Realtime voice call sessions"},
                {"id": "api", "label": "API", "description": "External API-triggered executions"},
            ],
            "channels": [
                {"id": "chat", "label": "Chat", "description": "In-session WebSocket approval prompt"},
                {"id": "phone", "label": "Phone Call", "description": "Outbound phone call verification via ACS"},
            ],
            "strategies": [
                {"id": "allow", "label": "Allow", "description": "Pass through without review", "color": "var(--ok)"},
                {"id": "deny", "label": "Deny", "description": "Block immediately", "color": "var(--err)"},
                {"id": "hitl", "label": "HITL", "description": "Human-in-the-loop approval via chat", "color": "var(--blue)"},
                {"id": "pitl", "label": "PITL (Experimental)", "description": "Phone-in-the-loop approval via outbound phone call (experimental)", "color": "var(--cyan, #22d3ee)"},
                {"id": "aitl", "label": "AITL", "description": "AI-in-the-loop: background reviewer agent decides", "color": "var(--gold)"},
                {"id": "filter", "label": "Filter", "description": "Content Safety Prompt Shields injection detection", "color": "var(--purple, #a78bfa)"},
            ],
        })

    async def _list_presets(self, _req: web.Request) -> web.Response:
        """Return available preset definitions with model-tier metadata."""
        return web.json_response({
            "status": "ok",
            "presets": list_presets(),
        })

    async def _apply_preset(self, req: web.Request) -> web.Response:
        """Apply a named preset to context_defaults and tool_policies."""
        preset_id = req.match_info["preset_id"]
        try:
            self._store.apply_preset(preset_id)
        except ValueError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )

        # If the request body includes models, also apply per-model defaults
        try:
            data = await req.json()
            models = data.get("models")
            if models and isinstance(models, list):
                self._store.apply_model_defaults(models, preset=preset_id)
        except Exception:
            pass  # No body or invalid JSON is fine

        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def _set_all(self, req: web.Request) -> web.Response:
        """Set all guardrails to a single strategy."""
        data = await req.json()
        strategy = data.get("strategy", "").strip()
        if not strategy:
            return web.json_response(
                {"status": "error", "message": "strategy is required"}, status=400
            )
        try:
            self._store.set_all_strategies(strategy)
        except ValueError as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)}, status=400
            )
        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def _apply_model_defaults(self, req: web.Request) -> web.Response:
        """Auto-populate model columns with tier-appropriate policies."""
        data = await req.json()
        models = data.get("models")
        if not models or not isinstance(models, list):
            return web.json_response(
                {"status": "error", "message": "models array is required"}, status=400
            )
        self._store.apply_model_defaults(models)
        return web.json_response({"status": "ok", **self._store.to_dict()})

    async def _list_model_tiers(self, _req: web.Request) -> web.Response:
        """Return all known models with their tier and recommended preset."""
        return web.json_response({
            "status": "ok",
            "models": list_model_tiers(),
        })

    async def _list_templates(self, _req: web.Request) -> web.Response:
        """Return the list of prompt template names."""
        from pathlib import Path as _Path

        from ...agent.prompt import _TEMPLATES_DIR

        templates: list[dict[str, str]] = []
        if _TEMPLATES_DIR.is_dir():
            for f in sorted(_TEMPLATES_DIR.iterdir()):
                if f.suffix == ".md":
                    templates.append({
                        "name": f.name,
                        "size": str(f.stat().st_size),
                    })
        # Also include SOUL.md if it exists
        from ...config.settings import cfg

        if cfg.soul_path.exists():
            templates.insert(0, {
                "name": "SOUL.md",
                "size": str(cfg.soul_path.stat().st_size),
            })
        return web.json_response({"status": "ok", "templates": templates})

    async def _get_template(self, req: web.Request) -> web.Response:
        """Fetch the content of a single prompt template."""
        name = req.match_info["name"]
        if ".." in name or "/" in name:
            return web.json_response(
                {"status": "error", "message": "invalid name"}, status=400
            )
        # Check SOUL.md first
        if name == "SOUL.md":
            from ...config.settings import cfg

            if cfg.soul_path.exists():
                return web.json_response({
                    "status": "ok",
                    "name": name,
                    "content": cfg.soul_path.read_text(),
                })
            return web.json_response(
                {"status": "error", "message": "not found"}, status=404
            )
        from ...agent.prompt import _TEMPLATES_DIR

        path = _TEMPLATES_DIR / name
        if not path.exists() or not path.suffix == ".md":
            return web.json_response(
                {"status": "error", "message": "not found"}, status=404
            )
        return web.json_response({
            "status": "ok",
            "name": name,
            "content": path.read_text(),
        })

    async def _list_background_agents(self, _req: web.Request) -> web.Response:
        """Return metadata for all background agents with current policy."""
        from ...state.guardrails_config import list_background_agents

        agents = list_background_agents()
        config = self._store.config
        # Annotate each agent with its current effective policy
        for agent in agents:
            agent_id = agent["id"]
            ctx_default = config.context_defaults.get(agent_id, "")
            has_override = bool(ctx_default)
            if has_override:
                effective = ctx_default
            else:
                # Fall back to background context default, then global
                effective = config.context_defaults.get(
                    "background", config.default_action,
                )
            agent["current_policy"] = effective
            agent["has_override"] = has_override
        return web.json_response({"status": "ok", "agents": agents})

    async def _get_policy_yaml(self, _req: web.Request) -> web.Response:
        """Return the current guardrails config as agent-policy YAML."""
        yaml_text = self._store.get_policy_yaml()
        return web.json_response({"status": "ok", "yaml": yaml_text})

    async def _put_policy_yaml(self, req: web.Request) -> web.Response:
        """Apply a raw agent-policy YAML, updating the guardrails config.

        Validates the YAML before applying.  On success the JSON config
        is regenerated from the YAML so both representations stay in sync.
        """
        data = await req.json()
        yaml_text = data.get("yaml", "")
        if not yaml_text:
            return web.json_response(
                {"status": "error", "message": "yaml field is required"}, status=400
            )
        error = self._store.set_policy_yaml(yaml_text)
        if error:
            return web.json_response(
                {"status": "error", "message": error}, status=400
            )
        return web.json_response({"status": "ok", **self._store.to_dict()})
