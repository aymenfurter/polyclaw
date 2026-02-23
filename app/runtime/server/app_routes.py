"""Route registration helpers for AppFactory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiohttp import web

from ..config.settings import cfg
from .app_static import voice_handler
from .wiring import create_voice_handler

if TYPE_CHECKING:
    from ..services.cloud.azure import AzureCLI
    from ..services.cloud.github import GitHubAuth
    from ..services.deployment.aca_deployer import AcaDeployer
    from ..services.deployment.deployer import BotDeployer
    from ..services.deployment.provisioner import Provisioner
    from ..services.tunnel import CloudflareTunnel
    from ..state.deploy_state import DeployStateStore
    from ..state.foundry_iq_config import FoundryIQConfigStore
    from ..state.guardrails import GuardrailsConfigStore
    from ..state.infra_config import InfraConfigStore
    from ..state.mcp_config import McpConfigStore
    from ..state.monitoring_config import MonitoringConfigStore
    from ..state.proactive import ProactiveStore
    from ..state.sandbox_config import SandboxConfigStore
    from ..sandbox import SandboxExecutor
    from ..messaging.proactive import ConversationReferenceStore
    from ..state.session_store import SessionStore
    from ..scheduler import Scheduler
    from .bot_endpoint import BotEndpoint


def register_admin_routes(
    router: web.UrlDispatcher,
    *,
    az: AzureCLI | None,
    gh: GitHubAuth | None,
    tunnel: CloudflareTunnel | None,
    deployer: BotDeployer | None,
    rebuild_adapter: Any,
    infra_store: InfraConfigStore | None,
    provisioner: Provisioner | None,
    deploy_store: DeployStateStore | None,
    aca_deployer: AcaDeployer | None,
    sandbox_store: SandboxConfigStore | None,
    sandbox_executor: SandboxExecutor | None,
    foundry_iq_store: FoundryIQConfigStore | None,
    monitoring_store: MonitoringConfigStore | None,
    guardrails_store: GuardrailsConfigStore | None,
) -> None:
    """Register routes available only in ``admin`` or ``combined`` mode."""
    from .setup import SetupRoutes, VoiceSetupRoutes
    from .workspace import WorkspaceHandler
    from .routes.content_safety_routes import ContentSafetyRoutes
    from .routes.env_routes import EnvironmentRoutes
    from .routes.foundry_iq_routes import FoundryIQRoutes
    from .routes.network_routes import NetworkRoutes
    from .routes.monitoring_routes import MonitoringRoutes
    from .routes.sandbox_routes import SandboxRoutes

    SetupRoutes(
        az, gh, tunnel, deployer,
        rebuild_adapter, infra_store,
        provisioner, deploy_store,
        aca_deployer,
    ).register(router)

    VoiceSetupRoutes(az, infra_store).register(router)
    WorkspaceHandler().register(router)
    EnvironmentRoutes(deploy_store, az).register(router)
    SandboxRoutes(sandbox_store, sandbox_executor, az, deploy_store).register(router)
    FoundryIQRoutes(foundry_iq_store, az, deploy_store).register(router)
    NetworkRoutes(tunnel, az, sandbox_store, foundry_iq_store).register(router)
    MonitoringRoutes(monitoring_store, az, deploy_store).register(router)
    ContentSafetyRoutes(az, guardrails_store).register(router)

    from .routes.identity_routes import IdentityRoutes

    IdentityRoutes(az, guardrails_store).register(router)

    if az:
        from .routes.security_preflight_routes import SecurityPreflightRoutes
        from ..services.security.security_preflight import SecurityPreflightChecker

        SecurityPreflightRoutes(SecurityPreflightChecker(az)).register(router)


def register_runtime_routes(
    app: web.Application,
    *,
    agent: Any,
    session_store: SessionStore | None,
    sandbox_executor: SandboxExecutor | None,
    mcp_store: McpConfigStore | None,
    guardrails_store: GuardrailsConfigStore | None,
    scheduler: Scheduler | None,
    proactive_store: ProactiveStore | None,
    adapter: Any,
    conv_store: ConversationReferenceStore | None,
    bot_ep: BotEndpoint | None,
    tunnel: CloudflareTunnel | None,
    voice_routes: Any,
    handle_reload: Any,
) -> None:
    """Register routes available only in ``runtime`` or ``combined`` mode."""
    from ..registries.plugins import get_plugin_registry
    from ..registries.skills import get_registry as get_skill_registry
    from ..state.plugin_config import PluginConfigStore
    from .chat import ChatHandler
    from .routes.guardrails_routes import GuardrailsRoutes
    from .routes.mcp_routes import McpRoutes
    from .routes.plugin_routes import PluginRoutes
    from .routes.proactive_routes import ProactiveRoutes
    from .routes.profile_routes import ProfileRoutes
    from .routes.scheduler_routes import SchedulerRoutes
    from .routes.session_routes import SessionRoutes
    from .routes.skill_routes import SkillRoutes
    from .routes.tool_activity_routes import ToolActivityRoutes

    router = app.router

    router.add_post("/api/internal/reload", handle_reload)

    from .routes.network_routes import NetworkRoutes as _NR

    _nr_instance = _NR(tunnel)
    router.add_get("/api/network/endpoints", _nr_instance._endpoints)

    hitl = agent.hitl_interceptor if agent else None
    if hitl:
        wire_hitl_services(app, hitl, guardrails_store)

    ChatHandler(
        agent,
        session_store=session_store,
        sandbox_interceptor=sandbox_executor,
        hitl_interceptor=hitl,
    ).register(router)

    bot_ep.register(router)
    register_voice_dynamic(app, voice_routes=voice_routes, agent=agent, tunnel=tunnel)

    SchedulerRoutes(scheduler).register(router)
    SessionRoutes(session_store).register(router)
    SkillRoutes(get_skill_registry()).register(router)
    McpRoutes(mcp_store).register(router)
    PluginRoutes(get_plugin_registry(), PluginConfigStore()).register(router)
    ProfileRoutes().register(router)
    GuardrailsRoutes(
        guardrails_store, mcp_store,
        skills_registry=get_skill_registry(),
    ).register(router)

    from ..state.tool_activity_store import get_tool_activity_store

    ToolActivityRoutes(get_tool_activity_store(), session_store).register(router)

    ProactiveRoutes(
        proactive_store,
        adapter=adapter,
        conv_store=conv_store,
        app_id=cfg.bot_app_id,
    ).register(router)


def wire_hitl_services(
    app: web.Application, hitl: Any, guardrails_store: Any,
) -> None:
    """Wire phone verifier, AITL reviewer, and prompt shield into HITL."""
    from ..agent.aitl import AitlReviewer
    from ..agent.phone_verify import PhoneVerifier
    from ..services.security.prompt_shield import PromptShieldService

    phone_verifier = PhoneVerifier(app)
    hitl.set_phone_verifier(phone_verifier)
    app["_phone_verifier"] = phone_verifier

    gcfg = guardrails_store.config
    hitl.set_aitl_reviewer(
        AitlReviewer(model=gcfg.aitl_model, spotlighting=gcfg.aitl_spotlighting),
    )
    hitl.set_prompt_shield(
        PromptShieldService(
            endpoint=gcfg.content_safety_endpoint, mode=gcfg.filter_mode,
        ),
    )


def register_voice_dynamic(
    app: web.Application,
    *,
    voice_routes: Any,
    agent: Any,
    tunnel: Any,
) -> None:
    """Register dynamic voice routes that delegate to the current handler."""
    app["_voice_handler"] = voice_routes

    def reinit_voice() -> None:
        handler = create_voice_handler(agent, tunnel)
        app["_voice_handler"] = handler
        app["voice_configured"] = handler is not None

    app["_reinit_voice"] = reinit_voice

    router = app.router
    router.add_post("/api/voice/call", voice_handler("_api_call"))
    router.add_get("/api/voice/status", voice_handler("_api_status"))
    # Legacy routes (kept for backwards compat)
    router.add_post("/acs", voice_handler("_acs_callback", log_label="ACS callback"))
    router.add_post("/acs/incoming", voice_handler("_acs_incoming", log_label="ACS incoming"))
    router.add_get(
        "/realtime-acs",
        voice_handler("_ws_handler_acs", log_label="ACS media-streaming WS"),
    )
    # Routes matching cfg.acs_callback_path / cfg.acs_media_streaming_websocket_path
    router.add_post(
        "/api/voice/acs-callback",
        voice_handler("_acs_callback", log_label="ACS callback"),
    )
    router.add_post(
        "/api/voice/acs-callback/incoming",
        voice_handler("_acs_incoming", log_label="ACS incoming"),
    )
    router.add_get(
        "/api/voice/media-streaming",
        voice_handler("_ws_handler_acs", log_label="ACS media-streaming WS"),
    )
