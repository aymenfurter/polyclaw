"""Service wiring -- initialises core components, stores, and external services."""

from __future__ import annotations

import logging
from typing import Any

from ..config.settings import ServerMode, cfg

logger = logging.getLogger(__name__)


def create_adapter() -> object:
    """Create a BotFrameworkAdapter with the current cfg credentials."""
    from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
    from botbuilder.schema import Activity, ActivityTypes

    settings = BotFrameworkAdapterSettings(
        app_id=cfg.bot_app_id or None,
        app_password=cfg.bot_app_password or None,
        channel_auth_tenant=cfg.bot_app_tenant_id or None,
    )
    adapter = BotFrameworkAdapter(settings)

    async def on_error(context: TurnContext, error: Exception) -> None:
        logger.error("Bot turn error: %s", error, exc_info=True)
        try:
            activity = Activity(type=ActivityTypes.message, text="An error occurred.")
            if (context.activity.channel_id or "").lower() == "telegram":
                activity.text_format = "plain"
            await context.send_activity(activity)
        except Exception:
            pass

    adapter.on_turn_error = on_error
    return adapter


def _append_token(url: str, token: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}token={token}"


def create_voice_handler(agent: object, tunnel: object | None = None) -> object | None:
    """Instantiate the ACS + Realtime voice handler, or ``None`` if not configured."""
    cfg.reload()
    if not (cfg.acs_connection_string and cfg.acs_source_number and cfg.azure_openai_endpoint):
        logger.info("Voice call not configured (ACS/AOAI settings missing)")
        return None

    from azure.core.credentials import AzureKeyCredential as _AKC

    from ..realtime import AcsCaller, RealtimeMiddleTier, RealtimeRoutes

    def _resolve_acs_urls() -> tuple[str, str]:
        token = cfg.acs_callback_token
        cb_path = cfg.acs_callback_path
        ws_path = cfg.acs_media_streaming_websocket_path

        logger.debug(
            "_resolve_acs_urls: cb_path=%r, ws_path=%r, token=%s",
            cb_path, ws_path, "set" if token else "empty",
        )

        # If both paths are already absolute URLs, use them directly
        cb_is_absolute = cb_path.startswith("https://")
        ws_is_absolute = ws_path.startswith("wss://")
        if cb_is_absolute and ws_is_absolute:
            resolved = _append_token(cb_path, token), _append_token(ws_path, token)
            logger.info("ACS URLs (absolute): callback=%s, ws=%s", resolved[0], resolved[1])
            return resolved

        # Otherwise, resolve relative paths against the tunnel URL
        tunnel_url = (getattr(tunnel, "url", None) or "").rstrip("/")
        if tunnel_url:
            cb = cb_path if cb_is_absolute else f"{tunnel_url}{cb_path or '/api/voice/acs-callback'}"
            ws = ws_path if ws_is_absolute else (
                tunnel_url.replace("https://", "wss://").replace("http://", "ws://")
                + (ws_path or "/api/voice/media-streaming")
            )
            resolved = _append_token(cb, token), _append_token(ws, token)
            logger.info("ACS URLs (tunnel): callback=%s, ws=%s", resolved[0], resolved[1])
            return resolved
        logger.warning("ACS URLs fallback to localhost -- calls will fail")
        return (
            cb_path or f"http://localhost:{cfg.admin_port}/api/voice/acs-callback",
            ws_path or f"ws://localhost:{cfg.admin_port}/api/voice/media-streaming",
        )

    caller = AcsCaller(
        source_number=cfg.acs_source_number,
        acs_connection_string=cfg.acs_connection_string,
        resolve_urls=_resolve_acs_urls,
        resolve_source_number=lambda: cfg.acs_source_number,
    )

    realtime_credential: _AKC | object
    if cfg.azure_openai_api_key:
        realtime_credential = _AKC(cfg.azure_openai_api_key)
    else:
        from azure.identity import DefaultAzureCredential as _DAC

        realtime_credential = _DAC()

    rt_middleware = RealtimeMiddleTier(
        endpoint=cfg.azure_openai_endpoint,
        deployment=cfg.azure_openai_realtime_deployment,
        credential=realtime_credential,
        agent=agent,
    )
    handler = RealtimeRoutes(
        caller,
        rt_middleware,
        callback_token=cfg.acs_callback_token,
        acs_resource_id=cfg.acs_resource_id,
    )
    logger.info("Voice call (ACS + Realtime) enabled: source=%s", cfg.acs_source_number)
    return handler


async def init_core(mode: ServerMode) -> dict[str, Any]:
    """Initialise the agent, adapter, bot, and session store.

    Returns a dict of component references keyed by name.
    """
    result: dict[str, Any] = {
        "agent": None,
        "adapter": None,
        "conv_store": None,
        "session_store": None,
        "bot": None,
        "bot_ep": None,
    }
    is_runtime = mode in (ServerMode.runtime, ServerMode.combined)
    is_admin = mode in (ServerMode.admin, ServerMode.combined)

    if is_runtime:
        from ..agent.agent import Agent
        from ..messaging.bot import Bot
        from ..messaging.proactive import ConversationReferenceStore
        from ..state.session_store import SessionStore
        from .bot_endpoint import BotEndpoint

        logger.info("[init_core] creating Agent ...")
        agent = Agent()
        logger.info("[init_core] starting Agent (Copilot CLI) ...")
        await agent.start()
        logger.info("[init_core] Agent started successfully")

        adapter = create_adapter()
        conv_store = ConversationReferenceStore()
        session_store = SessionStore()

        hitl = agent.hitl_interceptor
        bot = Bot(agent, conv_store, hitl=hitl)
        bot.session_store = session_store
        bot.adapter = adapter
        bot_ep = BotEndpoint(adapter, bot)
        logger.info("[init_core] core initialization complete")

        result.update(
            agent=agent, adapter=adapter, conv_store=conv_store,
            session_store=session_store, bot=bot, bot_ep=bot_ep,
        )

    if is_admin and not is_runtime:
        from ..state.session_store import SessionStore

        result["session_store"] = SessionStore()
        logger.info("[init_core] admin-only initialization complete")

    return result


def init_services(mode: ServerMode) -> dict[str, Any]:
    """Initialise state stores, cloud services, and background processors.

    Returns a dict of service/store references keyed by name.
    """
    from ..state.deploy_state import DeployStateStore
    from ..state.foundry_iq_config import FoundryIQConfigStore
    from ..state.guardrails import GuardrailsConfigStore
    from ..state.infra_config import InfraConfigStore
    from ..state.mcp_config import McpConfigStore
    from ..state.monitoring_config import MonitoringConfigStore
    from ..state.sandbox_config import SandboxConfigStore

    is_admin = mode in (ServerMode.admin, ServerMode.combined)
    is_runtime = mode in (ServerMode.runtime, ServerMode.combined)

    result: dict[str, Any] = {
        "tunnel": None,
        "deploy_store": DeployStateStore(),
        "infra_store": InfraConfigStore(),
        "mcp_store": McpConfigStore(),
        "sandbox_store": SandboxConfigStore(),
        "foundry_iq_store": FoundryIQConfigStore(),
        "guardrails_store": GuardrailsConfigStore(),
        "monitoring_store": MonitoringConfigStore(),
        "az": None,
        "gh": None,
        "deployer": None,
        "provisioner": None,
        "aca_deployer": None,
        "scheduler": None,
        "proactive_store": None,
        "sandbox_executor": None,
    }

    if is_runtime:
        from ..services.tunnel import CloudflareTunnel

        result["tunnel"] = CloudflareTunnel()

    # Admin-side services
    if is_admin:
        from ..services.cloud.azure import AzureCLI
        from ..services.cloud.github import GitHubAuth
        from ..services.deployment.aca_deployer import AcaDeployer
        from ..services.deployment.deployer import BotDeployer
        from ..services.deployment.provisioner import Provisioner

        az = AzureCLI()
        deployer = BotDeployer(az, result["deploy_store"])
        result.update(
            az=az,
            gh=GitHubAuth(),
            deployer=deployer,
            provisioner=Provisioner(
                az, deployer,
                result["infra_store"], result["deploy_store"],
                tunnel=result["tunnel"],
            ),
            aca_deployer=AcaDeployer(az, result["deploy_store"]),
        )
    elif is_runtime:
        from ..services.cloud.azure import AzureCLI
        from ..services.deployment.deployer import BotDeployer
        from ..services.deployment.provisioner import Provisioner

        az = AzureCLI()
        deployer = BotDeployer(az, result["deploy_store"])
        result.update(
            az=az,
            deployer=deployer,
            provisioner=Provisioner(
                az, deployer,
                result["infra_store"], result["deploy_store"],
                tunnel=result["tunnel"],
            ),
        )

    # Runtime-side services
    if is_runtime:
        from ..sandbox import SandboxExecutor
        from ..scheduler import get_scheduler
        from ..state.proactive import get_proactive_store

        result.update(
            scheduler=get_scheduler(),
            proactive_store=get_proactive_store(),
            sandbox_executor=SandboxExecutor(result["sandbox_store"]),
        )

    return result
