"""Web admin server -- app factory and entry point."""

from __future__ import annotations

import asyncio
import hmac
import logging
import mimetypes
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from .. import __version__
from ..config.settings import ServerMode, cfg
from ..media import EXTENSION_TO_MIME

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
_QUIET_PATHS = frozenset({"/api/setup/status", "/health"})


class QuietAccessLogger(AbstractAccessLogger):
    """Demotes polling-endpoint and noisy log entries to DEBUG."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        status = response.status
        if request.path in _QUIET_PATHS or status == 401 or status in (502, 503):
            level = logging.DEBUG
        else:
            level = logging.INFO
        self.logger.log(
            level,
            "%s %s %s %s %.3fs",
            request.remote,
            request.method,
            request.path,
            status,
            time,
        )


def create_adapter() -> object:
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


_PUBLIC_PREFIXES = ("/health", "/api/messages", "/acs", "/realtime-acs", "/api/voice/acs-callback", "/api/voice/media-streaming")
_PUBLIC_EXACT = ("/api/auth/check",)

_TUNNEL_ALLOWED_PREFIXES = (
    "/health",
    "/api/messages",
    "/acs",
    "/realtime-acs",
    "/api/voice/acs-callback",
    "/api/voice/media-streaming",
)

_LOCKDOWN_ALLOWED_PREFIXES = (
    "/health",
    "/api/messages",
    "/acs",
    "/realtime-acs",
    "/api/voice/acs-callback",
    "/api/voice/media-streaming",
    "/api/setup/lockdown",
)

_CF_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-ipcountry")


@web.middleware
async def lockdown_middleware(request: web.Request, handler):  # type: ignore[type-arg]
    if not cfg.lockdown_mode:
        return await handler(request)
    if any(request.path.startswith(p) for p in _LOCKDOWN_ALLOWED_PREFIXES):
        return await handler(request)
    return web.json_response(
        {
            "status": "locked",
            "message": (
                "Lock Down Mode is active. The admin panel is disabled. "
                "Use /lockdown off via the bot to restore access."
            ),
        },
        status=403,
    )


@web.middleware
async def tunnel_restriction_middleware(request: web.Request, handler):  # type: ignore[type-arg]
    if not cfg.tunnel_restricted:
        return await handler(request)
    is_tunnel = any(request.headers.get(h) for h in _CF_HEADERS)
    if not is_tunnel:
        return await handler(request)
    if any(request.path.startswith(p) for p in _TUNNEL_ALLOWED_PREFIXES):
        return await handler(request)
    return web.json_response({"status": "forbidden"}, status=403)


@web.middleware
async def auth_middleware(request: web.Request, handler):  # type: ignore[type-arg]
    secret = cfg.admin_secret
    if not secret:
        return await handler(request)

    path = request.path

    # Only protect /api/* endpoints (except public ones); frontend assets are public
    if not path.startswith("/api/"):
        return await handler(request)

    if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await handler(request)

    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    if hmac.compare_digest(auth, expected):
        return await handler(request)

    token_param = request.query.get("token", "")
    if token_param and hmac.compare_digest(token_param, secret):
        return await handler(request)

    secret_param = request.query.get("secret", "")
    if secret_param and hmac.compare_digest(secret_param, secret):
        return await handler(request)

    return web.json_response(
        {"status": "unauthorized", "message": "Invalid or missing admin secret"},
        status=401,
    )


def _append_token(url: str, token: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}token={token}"


async def create_app() -> web.Application:
    factory = AppFactory()
    return await factory.build()


def _create_voice_handler(agent: object, tunnel: object | None = None) -> object | None:
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

        logger.debug("_resolve_acs_urls: cb_path=%r, ws_path=%r, token=%s", cb_path, ws_path, "set" if token else "empty")

        # If both paths are already absolute URLs, use them directly
        cb_is_absolute = cb_path.startswith("https://")
        ws_is_absolute = ws_path.startswith("wss://")
        if cb_is_absolute and ws_is_absolute:
            resolved = _append_token(cb_path, token), _append_token(ws_path, token)
            logger.info("ACS URLs (absolute): callback=%s, ws=%s", resolved[0], resolved[1])
            return resolved

        # Otherwise, resolve relative paths against the tunnel URL
        tunnel_url = (getattr(tunnel, 'url', None) or "").rstrip("/")
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


_SCHEDULE_INTERVALS = {"hourly": 3600, "daily": 86400}


class AppFactory:

    async def build(self) -> web.Application:
        self._mode = cfg.server_mode
        cfg.ensure_dirs()
        self._ensure_admin_secret()
        await self._init_core()
        self._init_services()

        if self._bot and self._agent and self._agent.hitl_interceptor:
            self._bot._hitl = self._agent.hitl_interceptor
            self._bot._processor._hitl = self._agent.hitl_interceptor

        if self._scheduler and self._agent and self._agent.hitl_interceptor:
            self._scheduler.set_hitl_interceptor(self._agent.hitl_interceptor)
        if self._bot and self._scheduler:
            self._bot._scheduler = self._scheduler

        self._init_voice()

        middlewares = [lockdown_middleware, tunnel_restriction_middleware, auth_middleware]

        # Admin-only mode: proxy unmatched /api/* requests to runtime
        proxy_mw = None
        if self._is_admin and not self._is_runtime:
            from .runtime_proxy import create_runtime_proxy_middleware

            if os.getenv("POLYCLAW_USE_MI"):
                aca_fqdn = cfg.env.read("ACA_RUNTIME_FQDN")
                if aca_fqdn:
                    aca_url = f"https://{aca_fqdn}"
                    os.environ["RUNTIME_URL"] = aca_url
                    logger.info("[startup] Restored RUNTIME_URL=%s from ACA deployment", aca_url)

            proxy_mw = create_runtime_proxy_middleware()
            middlewares.append(proxy_mw)

        app = web.Application(middlewares=middlewares)
        app["voice_configured"] = self._voice_routes is not None
        app["server_mode"] = self._mode.value

        self._register_routes(app)
        self._register_lifecycle(app)

        if proxy_mw is not None:
            app.on_cleanup.append(proxy_mw.cleanup)

        return app

    @property
    def _is_admin(self) -> bool:
        return self._mode in (ServerMode.admin, ServerMode.combined)

    @property
    def _is_runtime(self) -> bool:
        return self._mode in (ServerMode.runtime, ServerMode.combined)

    @staticmethod
    def _ensure_admin_secret() -> None:
        if cfg.admin_secret:
            return
        if cfg.server_mode == ServerMode.runtime:
            logger.warning(
                "ADMIN_SECRET not set -- the admin container must start first "
                "and write the secret to the shared .env"
            )
            return
        cfg.write_env(ADMIN_SECRET=secrets.token_urlsafe(24))
        logger.info("Generated ADMIN_SECRET (persisted to .env)")

    async def _init_core(self) -> None:
        self._agent = None
        self._adapter = None
        self._conv_store = None
        self._session_store = None
        self._bot = None
        self._bot_ep = None

        if self._is_runtime:
            from ..agent.agent import Agent
            from ..messaging.bot import Bot
            from ..messaging.proactive import ConversationReferenceStore
            from ..state.session_store import SessionStore
            from .bot_endpoint import BotEndpoint

            logger.info("[init_core] creating Agent ...")
            self._agent = Agent()
            logger.info("[init_core] starting Agent (Copilot CLI) ...")
            await self._agent.start()
            logger.info("[init_core] Agent started successfully")

            self._adapter = create_adapter()
            self._conv_store = ConversationReferenceStore()
            self._session_store = SessionStore()

            hitl = self._agent.hitl_interceptor if self._agent else None
            self._bot = Bot(self._agent, self._conv_store, hitl=hitl)
            self._bot.session_store = self._session_store
            self._bot.adapter = self._adapter
            self._bot_ep = BotEndpoint(self._adapter, self._bot)
            logger.info("[init_core] core initialization complete")

        if self._is_admin and not self._is_runtime:
            from ..state.session_store import SessionStore

            self._session_store = SessionStore()
            logger.info("[init_core] admin-only initialization complete")

    def _init_services(self) -> None:
        from ..state.deploy_state import DeployStateStore
        from ..state.foundry_iq_config import FoundryIQConfigStore
        from ..state.guardrails_config import GuardrailsConfigStore
        from ..state.infra_config import InfraConfigStore
        from ..state.mcp_config import McpConfigStore
        from ..state.monitoring_config import MonitoringConfigStore
        from ..state.sandbox_config import SandboxConfigStore

        self._tunnel = None
        if self._is_runtime:
            from ..services.tunnel import CloudflareTunnel

            self._tunnel = CloudflareTunnel()
        self._deploy_store = DeployStateStore()
        self._infra_store = InfraConfigStore()
        self._mcp_store = McpConfigStore()
        self._sandbox_store = SandboxConfigStore()
        self._foundry_iq_store = FoundryIQConfigStore()
        self._guardrails_store = GuardrailsConfigStore()
        self._monitoring_store = MonitoringConfigStore()

        # Admin-side services: Azure CLI, GitHub auth, deployer, provisioner
        self._az = None
        self._gh = None
        self._deployer = None
        self._provisioner = None
        self._aca_deployer = None
        if self._is_admin:
            from ..services.aca_deployer import AcaDeployer
            from ..services.azure import AzureCLI
            from ..services.deployer import BotDeployer
            from ..services.github import GitHubAuth
            from ..services.provisioner import Provisioner

            self._az = AzureCLI()
            self._gh = GitHubAuth()
            self._deployer = BotDeployer(self._az, self._deploy_store)
            self._provisioner = Provisioner(
                self._az, self._deployer,
                self._infra_store, self._deploy_store,
                tunnel=self._tunnel,
            )
            self._aca_deployer = AcaDeployer(self._az, self._deploy_store)
        elif self._is_runtime:
            from ..services.azure import AzureCLI
            from ..services.deployer import BotDeployer
            from ..services.provisioner import Provisioner

            self._az = AzureCLI()
            self._deployer = BotDeployer(self._az, self._deploy_store)
            self._provisioner = Provisioner(
                self._az, self._deployer,
                self._infra_store, self._deploy_store,
                tunnel=self._tunnel,
            )

        # Runtime-side services: scheduler, sandbox, proactive
        self._scheduler = None
        self._proactive_store = None
        self._sandbox_executor = None
        if self._is_runtime:
            from ..sandbox import SandboxExecutor
            from ..scheduler import get_scheduler
            from ..state.proactive import get_proactive_store

            self._scheduler = get_scheduler()
            self._proactive_store = get_proactive_store()
            self._sandbox_executor = SandboxExecutor(self._sandbox_store)
            if self._agent:
                self._agent.set_sandbox(self._sandbox_executor)
                self._agent.set_guardrails(self._guardrails_store)

    def _init_voice(self) -> None:
        self._voice_routes = None
        if self._is_runtime:
            self._voice_routes = _create_voice_handler(self._agent, self._tunnel)

    def _rebuild_adapter(self) -> object:
        cfg.reload()
        self._adapter = create_adapter()
        if self._bot_ep:
            self._bot_ep.adapter = self._adapter
        if self._bot:
            self._bot.adapter = self._adapter
        logger.info(
            "Adapter rebuilt: app_id=%s, tenant=%s, password=%s",
            (cfg.bot_app_id[:12] + "...") if cfg.bot_app_id else "(none)",
            (cfg.bot_app_tenant_id[:12] + "...") if cfg.bot_app_tenant_id else "(none)",
            "set" if cfg.bot_app_password else "MISSING",
        )
        return self._adapter

    def _register_routes(self, app: web.Application) -> None:
        router = app.router

        async def auth_check(req: web.Request) -> web.Response:
            auth = req.headers.get("Authorization", "")
            ok = auth == f"Bearer {cfg.admin_secret}"
            return web.json_response({"authenticated": ok})

        router.add_post("/api/auth/check", auth_check)

        if self._is_admin:
            self._register_admin_routes(router)

        if self._is_runtime:
            self._register_runtime_routes(app)

        # Shared routes (both modes)
        router.add_get("/api/media/{filename:.+}", _serve_media)
        router.add_get("/health", self._health_handler())

        # Frontend SPA -- served by admin in split mode, or by combined
        if self._is_admin:
            self._register_frontend(router)

    def _register_admin_routes(self, router: web.UrlDispatcher) -> None:
        """Routes available only in ``admin`` or ``combined`` mode."""
        from .setup import SetupRoutes
        from .setup_voice import VoiceSetupRoutes
        from .workspace import WorkspaceHandler
        from .routes.content_safety_routes import ContentSafetyRoutes
        from .routes.env_routes import EnvironmentRoutes
        from .routes.foundry_iq_routes import FoundryIQRoutes
        from .routes.network_routes import NetworkRoutes
        from .routes.monitoring_routes import MonitoringRoutes
        from .routes.sandbox_routes import SandboxRoutes

        SetupRoutes(
            self._az, self._gh, self._tunnel, self._deployer,
            self._rebuild_adapter, self._infra_store,
            self._provisioner, self._deploy_store,
            self._aca_deployer,
        ).register(router)

        VoiceSetupRoutes(self._az, self._infra_store).register(router)
        WorkspaceHandler().register(router)
        EnvironmentRoutes(self._deploy_store, self._az).register(router)
        SandboxRoutes(
            self._sandbox_store, self._sandbox_executor, self._az, self._deploy_store,
        ).register(router)
        FoundryIQRoutes(self._foundry_iq_store, self._az, self._deploy_store).register(router)
        NetworkRoutes(self._tunnel, self._az, self._sandbox_store, self._foundry_iq_store).register(router)
        MonitoringRoutes(
            self._monitoring_store, self._az, self._deploy_store,
        ).register(router)
        ContentSafetyRoutes(self._az, self._guardrails_store).register(router)

        from .routes.identity_routes import IdentityRoutes
        IdentityRoutes(self._az, self._guardrails_store).register(router)

        if self._az:
            from .routes.security_preflight_routes import SecurityPreflightRoutes
            from ..services.security_preflight import SecurityPreflightChecker

            SecurityPreflightRoutes(SecurityPreflightChecker(self._az)).register(router)

    def _register_runtime_routes(self, app: web.Application) -> None:
        """Routes available only in ``runtime`` or ``combined`` mode."""
        from ..agent.aitl import AitlReviewer
        from ..agent.phone_verify import PhoneVerifier
        from ..registries.plugins import get_plugin_registry
        from ..registries.skills import get_registry as get_skill_registry
        from ..services.prompt_shield import PromptShieldService
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

        router.add_post("/api/internal/reload", self._handle_reload)

        from .routes.network_routes import NetworkRoutes as _NR
        _nr_instance = _NR(self._tunnel)
        router.add_get("/api/network/endpoints", _nr_instance._endpoints)

        hitl = self._agent.hitl_interceptor if self._agent else None

        # Wire phone verifier into HITL interceptor
        if hitl:
            phone_verifier = PhoneVerifier(app)
            hitl.set_phone_verifier(phone_verifier)
            app["_phone_verifier"] = phone_verifier

            # Wire AITL reviewer
            gcfg = self._guardrails_store.config
            aitl_reviewer = AitlReviewer(
                model=gcfg.aitl_model,
                spotlighting=gcfg.aitl_spotlighting,
            )
            hitl.set_aitl_reviewer(aitl_reviewer)

            prompt_shield = PromptShieldService(
                endpoint=gcfg.content_safety_endpoint,
                mode=gcfg.filter_mode,
            )
            hitl.set_prompt_shield(prompt_shield)

        ChatHandler(
            self._agent,
            session_store=self._session_store,
            sandbox_interceptor=self._sandbox_executor,
            hitl_interceptor=hitl,
        ).register(router)

        self._bot_ep.register(router)
        self._register_voice_dynamic(app)

        SchedulerRoutes(self._scheduler).register(router)
        SessionRoutes(self._session_store).register(router)
        SkillRoutes(get_skill_registry()).register(router)
        McpRoutes(self._mcp_store).register(router)
        PluginRoutes(get_plugin_registry(), PluginConfigStore()).register(router)
        ProfileRoutes().register(router)
        GuardrailsRoutes(
            self._guardrails_store, self._mcp_store,
            skills_registry=get_skill_registry(),
        ).register(router)

        from ..state.tool_activity_store import get_tool_activity_store
        ToolActivityRoutes(get_tool_activity_store(), self._session_store).register(router)

        ProactiveRoutes(
            self._proactive_store,
            adapter=self._adapter,
            conv_store=self._conv_store,
            app_id=cfg.bot_app_id,
        ).register(router)

    def _health_handler(self) -> Callable:
        """Return a health handler that includes mode and tunnel info."""
        mode = self._mode

        async def handler(_req: web.Request) -> web.Response:
            body: dict = {"status": "ok", "version": __version__, "mode": mode.value}
            if mode in (ServerMode.runtime, ServerMode.combined):
                body["tunnel_url"] = getattr(self._tunnel, "url", None) or ""
            return web.json_response(body)

        return handler

    def _register_frontend(self, router: web.UrlDispatcher) -> None:
        fe = _FRONTEND_DIR
        if not fe.exists():
            return
        router.add_get("/", _serve_index)
        if (fe / "assets").is_dir():
            router.add_static("/assets/", path=str(fe / "assets"), name="fe_assets")
        for fname in ("favicon.ico", "logo.png", "headertext.png"):
            fpath = fe / fname
            if fpath.exists():
                router.add_get(f"/{fname}", _make_file_handler(fpath))
        router.add_get("/{tail:[^/].*}", _serve_spa_or_404)

    def _register_voice_dynamic(self, app: web.Application) -> None:
        app["_voice_handler"] = self._voice_routes
        agent = self._agent

        def reinit_voice() -> None:
            handler = _create_voice_handler(agent, self._tunnel)
            app["_voice_handler"] = handler
            app["voice_configured"] = handler is not None

        app["_reinit_voice"] = reinit_voice

        def _not_configured() -> web.Response:
            return web.json_response(
                {
                    "status": "error",
                    "message": (
                        "Voice calling is not configured. Deploy ACS + "
                        "Azure OpenAI resources in the Voice Call section first."
                    ),
                },
                status=400,
            )

        async def voice_call(req: web.Request) -> web.Response:
            h = req.app["_voice_handler"]
            return _not_configured() if h is None else await h._api_call(req)

        async def voice_status(req: web.Request) -> web.Response:
            h = req.app["_voice_handler"]
            return _not_configured() if h is None else await h._api_status(req)

        async def acs_callback(req: web.Request) -> web.Response:
            h = req.app["_voice_handler"]
            logger.info("ACS callback hit: method=%s path=%s handler=%s", req.method, req.path, "configured" if h else "NONE")
            return _not_configured() if h is None else await h._acs_callback(req)

        async def acs_incoming(req: web.Request) -> web.Response:
            h = req.app["_voice_handler"]
            logger.info("ACS incoming hit: method=%s path=%s handler=%s", req.method, req.path, "configured" if h else "NONE")
            return _not_configured() if h is None else await h._acs_incoming(req)

        async def ws_handler_acs(req: web.Request) -> web.WebSocketResponse:
            h = req.app["_voice_handler"]
            logger.info("ACS media-streaming WS hit: method=%s path=%s handler=%s", req.method, req.path, "configured" if h else "NONE")
            return _not_configured() if h is None else await h._ws_handler_acs(req)  # type: ignore[return-value]

        router = app.router
        router.add_post("/api/voice/call", voice_call)
        router.add_get("/api/voice/status", voice_status)
        # Legacy routes (kept for backwards compat)
        router.add_post("/acs", acs_callback)
        router.add_post("/acs/incoming", acs_incoming)
        router.add_get("/realtime-acs", ws_handler_acs)
        # Routes matching cfg.acs_callback_path / cfg.acs_media_streaming_websocket_path
        router.add_post("/api/voice/acs-callback", acs_callback)
        router.add_post("/api/voice/acs-callback/incoming", acs_incoming)
        router.add_get("/api/voice/media-streaming", ws_handler_acs)

    def _make_notify(self) -> Callable[[str], Awaitable[bool]]:
        from ..messaging.proactive import send_proactive_message

        async def notify(message: str) -> bool:
            return await send_proactive_message(
                self._adapter, self._conv_store, cfg.bot_app_id, message,
            )

        return notify

    def _register_lifecycle(self, app: web.Application) -> None:
        if self._is_runtime and self._scheduler:
            from ..messaging.proactive import send_proactive_message

            async def notify(message: str) -> None:
                await send_proactive_message(
                    self._adapter, self._conv_store, cfg.bot_app_id, message,
                )

            self._scheduler.set_notify_callback(notify)

        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)

    async def _on_startup(self, app: web.Application) -> None:
        if self._is_runtime:
            await self._on_startup_runtime(app)
        if self._is_admin:
            await self._on_startup_admin(app)

    async def _on_startup_runtime(self, app: web.Application) -> None:
        """Start background tasks and bot infrastructure for the runtime."""
        from ..proactive_loop import proactive_delivery_loop
        from ..scheduler import scheduler_loop
        from ..services.otel import configure_otel

        # Bootstrap OTel if monitoring is configured
        mon = self._monitoring_store
        if mon.is_configured:
            configure_otel(
                mon.connection_string,
                sampling_ratio=mon.config.sampling_ratio,
                enable_live_metrics=mon.config.enable_live_metrics,
            )

        self._rebuild_adapter()

        app["scheduler_task"] = asyncio.create_task(scheduler_loop())
        app["proactive_task"] = asyncio.create_task(
            proactive_delivery_loop(self._make_notify(), session_store=self._session_store),
        )
        app["foundry_iq_task"] = asyncio.create_task(
            _foundry_iq_index_loop(self._foundry_iq_store),
        )

        logger.info(
            "[startup.runtime] mode=%s lockdown=%s bot_configured=%s "
            "telegram_configured=%s tunnel=%s provisioner=%s az=%s",
            self._mode.value, cfg.lockdown_mode,
            self._infra_store.bot_configured if self._infra_store else "<no store>",
            self._infra_store.telegram_configured if self._infra_store else "<no store>",
            self._tunnel is not None,
            self._provisioner is not None,
            self._az is not None,
        )

        if cfg.lockdown_mode:
            logger.info("Lock Down Mode active -- skipping infrastructure provisioning")
            return

        bot_endpoint = os.environ.get("BOT_ENDPOINT", "")

        if self._mode != ServerMode.combined:
            github_token = cfg.github_token
            if not github_token:
                logger.warning(
                    "[startup.runtime] Setup incomplete -- missing GITHUB_TOKEN. "
                    "Complete the setup wizard in the admin container, "
                    "then recreate the agent container.",
                )
                return

        needs_bot = (
            self._infra_store.bot_configured
            and self._infra_store.telegram_configured
        )

        if self._mode == ServerMode.combined:
            if self._infra_store.bot_configured and self._provisioner:
                from ..util.async_helpers import run_sync

                logger.info("Startup: provisioning infrastructure from config ...")
                steps = await run_sync(self._provisioner.provision)
                self._rebuild_adapter()
                for s in steps:
                    logger.info(
                        "  provision: %s = %s (%s)",
                        s.get("step"), s.get("status"), s.get("detail", ""),
                    )
            if needs_bot and self._tunnel:
                await self._start_tunnel_and_create_bot()

        elif bot_endpoint:
            cfg.reload()
            self._rebuild_adapter()
            if needs_bot:
                logger.info("Static bot endpoint: %s", bot_endpoint)
                await self._recreate_bot(endpoint_override=bot_endpoint)
            else:
                logger.info("No messaging channels configured -- skipping bot service")

        else:
            if needs_bot and self._tunnel:
                from ..services.deployer import BotDeployer

                bot_app_id = BotDeployer._env("BOT_APP_ID")
                if not bot_app_id:
                    logger.warning(
                        "Telegram configured but BOT_APP_ID missing -- "
                        "run Infrastructure Deploy in the admin wizard first"
                    )
                else:
                    await self._start_tunnel_and_create_bot()
            else:
                reasons = []
                if not self._infra_store.bot_configured:
                    reasons.append("bot not configured")
                if not self._infra_store.telegram_configured:
                    reasons.append("no channels configured")
                if not self._tunnel:
                    reasons.append("no tunnel")
                logger.info(
                    "Skipping bot service: %s",
                    ", ".join(reasons) or "no reason",
                )

    async def _on_startup_admin(self, app: web.Application) -> None:
        """Admin startup: reconcile stale deployments and RBAC."""
        if self._az:
            from ..services.resource_tracker import ResourceTracker
            from ..util.async_helpers import run_sync

            app["reconcile_task"] = asyncio.create_task(self._reconcile_deployments())
            app["cs_rbac_task"] = asyncio.create_task(
                self._ensure_content_safety_rbac(),
            )

    async def _ensure_content_safety_rbac(self) -> None:
        from .routes.content_safety_routes import ContentSafetyRoutes

        try:
            routes = ContentSafetyRoutes(
                az=self._az,
                guardrails_store=self._guardrails_store,
            )
            steps = await routes.ensure_rbac()
            for s in steps:
                logger.info(
                    "[startup.cs_rbac] %s = %s (%s)",
                    s.get("step"), s.get("status"), s.get("detail", ""),
                )
        except Exception:
            logger.warning(
                "[startup.cs_rbac] Content Safety RBAC check failed",
                exc_info=True,
            )

    async def _recreate_bot(self, *, endpoint_override: str | None = None) -> None:
        from ..util.async_helpers import run_sync

        logger.info(
            "[recreate_bot] provisioner=%s az=%s bot_configured=%s endpoint_override=%s",
            self._provisioner is not None,
            self._az is not None,
            self._infra_store.bot_configured if self._infra_store else "?",
            endpoint_override,
        )
        if not (self._provisioner and self._az and self._infra_store.bot_configured):
            logger.warning(
                "[recreate_bot] precondition failed -- provisioner=%s az=%s bot_configured=%s",
                self._provisioner is not None,
                self._az is not None,
                self._infra_store.bot_configured if self._infra_store else "?",
            )
            return

        tunnel_url = endpoint_override or getattr(self._tunnel, "url", None)
        if not tunnel_url:
            logger.warning("Bot recreate: no endpoint URL available -- skipping")
            return

        endpoint = tunnel_url
        logger.info("Bot recreate: endpoint %s", endpoint)
        try:
            steps = await run_sync(self._provisioner.recreate_endpoint, endpoint)
            self._rebuild_adapter()
            for s in steps:
                logger.info(
                    "  recreate: %s = %s (%s)",
                    s.get("step"), s.get("status"), s.get("detail", ""),
                )
        except Exception as exc:
            logger.warning("Bot recreate: error -- %s", exc, exc_info=True)

    async def _start_tunnel_and_create_bot(self) -> None:
        from ..util.async_helpers import run_sync

        logger.info("Starting tunnel for bot service endpoint ...")
        tunnel_url = self._tunnel.url
        if not tunnel_url and not self._tunnel.is_active:
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                result = await run_sync(self._tunnel.start, cfg.admin_port)
                if result:
                    logger.info("Tunnel started at %s", result.value)
                    break
                if attempt < max_retries:
                    logger.warning(
                        "Tunnel failed (attempt %d/%d): %s -- retrying in %ds ...",
                        attempt, max_retries,
                        result.message if result else "unknown",
                        2 * attempt,
                    )
                    await asyncio.sleep(2 * attempt)
                else:
                    logger.error(
                        "Tunnel failed after %d attempts: %s",
                        max_retries,
                        result.message if result else "unknown",
                    )
                    return

        self._rebuild_adapter()
        await self._recreate_bot()

    async def _handle_reload(self, request: web.Request) -> web.Response:
        logger.info("[reload] triggered by admin -- re-reading configuration")

        # 1. Re-read .env from shared volume
        cfg.reload()

        # 2. Reload infra config (bot & channel settings from infra.json)
        if self._infra_store:
            self._infra_store._load()

        # 3. Reload agent auth (GITHUB_TOKEN may have changed)
        auth_result: dict = {}
        if self._agent:
            auth_result = await self._agent.reload_auth()
            logger.info("[reload] agent auth: %s", auth_result.get("status"))

        # 4. Rebuild Bot Framework adapter (BOT_APP_ID/PASSWORD may have changed)
        self._rebuild_adapter()

        # 5. Reinitialise voice handler (ACS settings may have changed)
        reinit_voice = request.app.get("_reinit_voice")
        if reinit_voice:
            reinit_voice()

        bot_task_started = False
        needs_bot = (
            self._infra_store
            and self._infra_store.bot_configured
            and self._infra_store.telegram_configured
        )
        if needs_bot:
            bot_endpoint = os.environ.get("BOT_ENDPOINT", "")
            tunnel_active = getattr(self._tunnel, "is_active", False) if self._tunnel else False

            if bot_endpoint:
                # Static endpoint (ACA or other) -- no tunnel needed.
                async def _deferred_static_bot() -> None:
                    await self._recreate_bot(endpoint_override=bot_endpoint)

                request.app["reload_bot_task"] = asyncio.create_task(
                    _deferred_static_bot()
                )
                bot_task_started = True
            elif self._tunnel and not tunnel_active:
                from ..services.deployer import BotDeployer

                bot_app_id = BotDeployer._env("BOT_APP_ID")
                if bot_app_id:
                    async def _deferred_docker_bot() -> None:
                        await self._start_tunnel_and_create_bot()

                    request.app["reload_bot_task"] = asyncio.create_task(
                        _deferred_docker_bot()
                    )
                    bot_task_started = True
            elif self._tunnel and tunnel_active:
                async def _deferred_recreate() -> None:
                    await self._recreate_bot()

                request.app["reload_bot_task"] = asyncio.create_task(
                    _deferred_recreate()
                )
                bot_task_started = True

        logger.info(
            "[reload] complete: auth=%s adapter=rebuilt bot_task=%s",
            auth_result.get("status", "n/a"),
            bot_task_started,
        )
        return web.json_response({
            "status": "ok",
            "auth": auth_result,
            "adapter_rebuilt": True,
            "bot_task_started": bot_task_started,
        })

    async def _reconcile_deployments(self) -> None:
        from ..services.resource_tracker import ResourceTracker
        from ..util.async_helpers import run_sync

        try:
            tracker = ResourceTracker(self._az, self._deploy_store)
            cleaned = await run_sync(tracker.reconcile)
            if cleaned:
                logger.info(
                    "Startup reconcile: removed %d stale deployment(s): %s",
                    len(cleaned), ", ".join(c["deploy_id"] for c in cleaned),
                )
        except Exception as exc:
            logger.warning("Startup reconcile failed (non-fatal): %s", exc)

    async def _on_cleanup(self, _app: web.Application) -> None:
        for key in ("scheduler_task", "proactive_task", "foundry_iq_task", "reconcile_task"):
            task = _app.get(key)
            if task and not task.done():
                task.cancel()

        if self._mode == ServerMode.combined:
            if cfg.lockdown_mode:
                logger.info("Lock Down Mode active -- skipping shutdown decommission")
            elif self._infra_store.bot_configured and (cfg.env.read("BOT_NAME") or cfg.env.read("BOT_APP_ID")) and self._provisioner:
                from ..util.async_helpers import run_sync

                logger.info("Shutdown: decommissioning infrastructure ...")
                steps = await run_sync(self._provisioner.decommission)
                for s in steps:
                    logger.info(
                        "  decommission: %s = %s (%s)",
                        s.get("step"), s.get("status"), s.get("detail", ""),
                    )

        if self._agent:
            await self._agent.stop()


async def _foundry_iq_index_loop(store: object) -> None:
    from ..services.foundry_iq import index_memories
    from ..state.foundry_iq_config import FoundryIQConfigStore
    from ..util.async_helpers import run_sync

    assert isinstance(store, FoundryIQConfigStore)
    await asyncio.sleep(60)
    while True:
        try:
            store._load()
            schedule = store.config.index_schedule
            if store.enabled and store.is_configured and schedule in _SCHEDULE_INTERVALS:
                logger.info("Foundry IQ: running scheduled indexing (%s)...", schedule)
                result = await run_sync(index_memories, store)
                logger.info("Foundry IQ indexing: %s (indexed=%s)", result.get("status"), result.get("indexed", 0))
            interval = _SCHEDULE_INTERVALS.get(schedule, 86400)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Foundry IQ index loop error: %s", exc, exc_info=True)
            interval = 3600
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


async def _serve_media(req: web.Request) -> web.Response:
    filename = req.match_info["filename"]
    if ".." in filename or filename.startswith("/"):
        return web.Response(status=403, text="Forbidden")
    file_path = cfg.media_outgoing_sent_dir / filename
    if not file_path.is_file():
        return web.Response(status=404, text="Not found")
    content_type = (
        EXTENSION_TO_MIME.get(file_path.suffix.lower())
        or mimetypes.guess_type(file_path.name)[0]
        or "application/octet-stream"
    )
    return web.FileResponse(file_path, headers={"Content-Type": content_type})


def _make_file_handler(fpath: Path):
    async def handler(_req: web.Request) -> web.Response:
        ct = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
        return web.FileResponse(fpath, headers={"Content-Type": ct})
    return handler


async def _serve_index(req: web.Request) -> web.Response:
    index = _FRONTEND_DIR / "index.html"
    if not index.exists():
        return web.Response(status=404, text="Not found")
    html = index.read_text()
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def _serve_spa_or_404(req: web.Request) -> web.Response:
    if req.path.startswith("/api/"):
        raise web.HTTPNotFound(
            text='{"status":"error","message":"Unknown endpoint: '
            f'{req.method} {req.path}"' + '}',
            content_type="application/json",
        )
    return await _serve_index(req)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Polyclaw server")
    parser.add_argument(
        "--admin-only",
        action="store_true",
        help="Run the admin control-plane only (no agent, no bot endpoint).",
    )
    parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Run the agent runtime data-plane only (no setup wizard, no Azure CLI).",
    )
    args = parser.parse_args()

    if args.admin_only and args.runtime_only:
        raise SystemExit("Cannot specify both --admin-only and --runtime-only")

    # Set the mode via env var so Settings.reload() picks it up.
    if args.admin_only:
        import os
        os.environ["POLYCLAW_SERVER_MODE"] = "admin"
    elif args.runtime_only:
        import os
        os.environ["POLYCLAW_SERVER_MODE"] = "runtime"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )
    from ..services.otel import _quiet_noisy_loggers

    _quiet_noisy_loggers()
    cfg.reload()
    port = cfg.admin_port
    mode = cfg.server_mode
    logger.info("Starting server in %s mode on port %d ...", mode.value, port)

    if not cfg.admin_secret:
        cfg.write_env(ADMIN_SECRET=secrets.token_urlsafe(24))
        logger.info("Generated ADMIN_SECRET (persisted to .env)")

    display_secret = cfg.admin_secret if cfg.admin_secret and not cfg.admin_secret.startswith("@kv:") else ""
    if mode == ServerMode.runtime:
        admin_url = f"http://localhost:{port}"
        logger.info("Runtime endpoint: %s", admin_url)
    elif display_secret:
        admin_url = f"http://localhost:{port}/?secret={display_secret}"
        logger.info("Admin UI: %s", admin_url)
    else:
        admin_url = f"http://localhost:{port}  (secret pending KV resolution)"
        logger.info("Admin UI: %s", admin_url)

    # Print to stdout so the URL is always visible regardless of log noise.
    print(f"\n  --> {admin_url}\n", flush=True)

    web.run_app(create_app(), host="0.0.0.0", port=port, access_log_class=QuietAccessLogger)


if __name__ == "__main__":
    main()
