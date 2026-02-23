"""System prompt builder -- assembles soul, operating manual, and context."""

from __future__ import annotations

from pathlib import Path

from ..config.settings import cfg

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _load_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text()


def load_soul() -> str:
    if not cfg.soul_path.exists():
        cfg.soul_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.soul_path.write_text(_load_template("placeholder_soul.md"))
    return cfg.soul_path.read_text()


def soul_exists() -> bool:
    if not cfg.soul_path.exists():
        return False
    return "placeholder identity" not in cfg.soul_path.read_text().lower()


def _load_mcp_guidance() -> dict[str, str]:
    raw = _load_template("mcp_guidance.md")
    entries: dict[str, str] = {}
    for block in raw.split("\n---\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("**"):
            end = block.index("**", 2)
            name = block[2:end].strip().lower().replace(" ", "-")
            entries[name] = block
    return entries


_MCP_GUIDANCE: dict[str, str] | None = None


def _get_mcp_guidance() -> dict[str, str]:
    global _MCP_GUIDANCE
    if _MCP_GUIDANCE is None:
        _MCP_GUIDANCE = _load_mcp_guidance()
    return _MCP_GUIDANCE


def _build_mcp_section() -> str:
    from ..state.mcp_config import McpConfigStore

    try:
        store = McpConfigStore()
        servers = store.list_servers()
    except Exception:
        return "_No MCP servers configured._"

    enabled = [s for s in servers if s.get("enabled", False)]
    if not enabled:
        return "_No MCP servers currently enabled._"

    guidance = _get_mcp_guidance()
    lines: list[str] = []
    for srv in enabled:
        name = srv["name"]
        if name in guidance:
            lines.append(f"- {guidance[name]}")
        else:
            srv_type = srv.get("type", "unknown")
            desc = srv.get("description", "No description")
            lines.append(f"- **{name}** ({srv_type}) -- {desc}")
    return "\n".join(lines)


def _build_sandbox_section() -> str:
    from ..state.sandbox_config import SandboxConfigStore

    try:
        store = SandboxConfigStore()
        if not store.config.enabled:
            return ""
    except Exception:
        return ""
    return "\n\n---\n\n" + _load_template("sandbox_prompt.md")


def build_system_prompt() -> str:
    from ..state.profile import load_profile

    soul_content = load_soul()

    bootstrap_section = ""
    if not soul_exists():
        bootstrap_section = (
            _load_template("bootstrap_prompt.md").format(
                soul_path=cfg.soul_path,
                profile_path=cfg.data_dir / "agent_profile.json",
            )
            + "\n\n---\n\n"
        )

    profile = load_profile()
    profile_lines: list[str] = []
    if profile.get("emotional_state") and profile["emotional_state"] != "neutral":
        profile_lines.append(
            f"Your current emotional state is: **{profile['emotional_state']}**. "
            "Let this subtly influence your tone and energy."
        )
    if profile.get("name"):
        profile_lines.append(f"Your chosen name is: **{profile['name']}**.")
    if profile.get("location"):
        profile_lines.append(f"Your location: {profile['location']}.")
    if profile.get("preferences"):
        pref_items = ", ".join(f"{k}: {v}" for k, v in profile["preferences"].items())
        profile_lines.append(f"Known user preferences: {pref_items}.")

    profile_section = "\n".join(profile_lines)
    if profile_section:
        profile_section = "\n\n---\n\n## Current State\n\n" + profile_section + "\n"

    mcp_section = _build_mcp_section()
    sandbox_section = _build_sandbox_section()

    return (
        _get_system_prompt_template().format(
            bootstrap=bootstrap_section,
            soul=soul_content,
            memory_dir=cfg.memory_dir,
            memory_daily_dir=cfg.memory_daily_dir,
            memory_topics_dir=cfg.memory_topics_dir,
            builtin_skills_dir=cfg.builtin_skills_dir,
            user_skills_dir=cfg.user_skills_dir,
            media_incoming_dir=cfg.media_incoming_dir,
            media_outgoing_dir=cfg.media_outgoing_dir,
            media_outgoing_pending_dir=cfg.media_outgoing_pending_dir,
            media_outgoing_sent_dir=cfg.media_outgoing_sent_dir,
            media_outgoing_error_dir=cfg.media_outgoing_error_dir,
            data_dir=cfg.data_dir,
            soul_path=cfg.soul_path,
            mcp_servers=mcp_section,
        )
        + profile_section
        + sandbox_section
    )


_SYSTEM_PROMPT_TEMPLATE: str | None = None


def _get_system_prompt_template() -> str:
    global _SYSTEM_PROMPT_TEMPLATE
    if _SYSTEM_PROMPT_TEMPLATE is None:
        _SYSTEM_PROMPT_TEMPLATE = _load_template("system_prompt.md")
    return _SYSTEM_PROMPT_TEMPLATE


def _reset_prompt_template() -> None:
    global _SYSTEM_PROMPT_TEMPLATE, _MCP_GUIDANCE
    _SYSTEM_PROMPT_TEMPLATE = None
    _MCP_GUIDANCE = None


from ..util.singletons import register_singleton
register_singleton(_reset_prompt_template)
