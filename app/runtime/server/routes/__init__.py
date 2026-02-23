"""Server route handlers."""

from __future__ import annotations

from .content_safety_routes import ContentSafetyRoutes
from .env_routes import EnvironmentRoutes
from .foundry_iq_routes import FoundryIQRoutes
from .guardrails_routes import GuardrailsRoutes
from .identity_routes import IdentityRoutes
from .mcp_routes import McpRoutes
from .monitoring_routes import MonitoringRoutes
from .network_routes import NetworkRoutes
from .plugin_routes import PluginRoutes
from .proactive_routes import ProactiveRoutes
from .profile_routes import ProfileRoutes
from .sandbox_routes import SandboxRoutes
from .scheduler_routes import SchedulerRoutes
from .security_preflight_routes import SecurityPreflightRoutes
from .session_routes import SessionRoutes
from .skill_routes import SkillRoutes
from .tool_activity_routes import ToolActivityRoutes

__all__ = [
    "ContentSafetyRoutes",
    "EnvironmentRoutes",
    "FoundryIQRoutes",
    "GuardrailsRoutes",
    "IdentityRoutes",
    "McpRoutes",
    "MonitoringRoutes",
    "NetworkRoutes",
    "PluginRoutes",
    "ProactiveRoutes",
    "ProfileRoutes",
    "SandboxRoutes",
    "SchedulerRoutes",
    "SecurityPreflightRoutes",
    "SessionRoutes",
    "SkillRoutes",
    "ToolActivityRoutes",
]
