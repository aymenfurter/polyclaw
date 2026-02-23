"""Web admin server -- app factory and entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .. import __version__
from ..config.settings import ServerMode, cfg

if TYPE_CHECKING:
    from ..agent.agent import Agent
    from ..messaging.bot import Bot
    from ..messaging.proactive import ConversationReferenceStore
    from ..sandbox import SandboxExecutor
    from ..scheduler import Scheduler
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
    from ..state.session_store import SessionStore
    from .bot_endpoint import BotEndpoint
from . import lifecycle
from .app_routes import register_admin_routes, register_runtime_routes
from .app_static import (
    FRONTEND_DIR,
    make_file_handler,
    serve_index,
    serve_media,
    serve_spa_or_404,
)
from .middleware import (
    auth_middleware,
    lockdown_middleware,
    tunnel_restriction_middleware,
)
from .wiring import create_adapter, create_voice_handler, init_core, init_services

logger = logging.getLogger(__name__)


async def create_app() -> web.Application:
    """Public entry point -- build and return the ``aiohttp`` application."""
    factory = AppFactory()
    return await factory.build()


class AppFactory:
    """Assembles the aiohttp application with routes, middleware, and lifecycle hooks.

    All dependency references are declared in ``__init__`` so the full shape
    of the object is visible in one place.
    """

    def __init__(self) -> None:
        self._mode: ServerMode = cfg.server_mode

        # Core components (populated by _init_core)
        self._agent: Agent | None = None
        self._adapter: Any = None  # BotFrameworkAdapter (external)
        self._conv_store: ConversationReferenceStore | None = None
        self._session_store: SessionStore | None = None
        self._bot: Bot | None = None
        self._bot_ep: BotEndpoint | None = None

        # State stores (populated by _init_services)
        self._deploy_store: DeployStateStore | None = None
        self._infra_store: InfraConfigStore | None = None
        self._mcp_store: McpConfigStore | None = None
        self._sandbox_store: SandboxConfigStore | None = None
        self._foundry_iq_store: FoundryIQConfigStore | None = None
        self._guardrails_store: GuardrailsConfigStore | None = None
        self._monitoring_store: MonitoringConfigStore | None = None

        # External services (populated by _init_services)
        self._tunnel: CloudflareTunnel | None = None
        self._az: AzureCLI | None = None
        self._gh: GitHubAuth | None = None
        self._deployer: BotDeployer | None = None
        self._provisioner: Provisioner | None = None
        self._aca_deployer: AcaDeployer | None = None

        # Runtime-only services (populated by _init_services)
        self._scheduler: Scheduler | None = None
        self._proactive_store: ProactiveStore | None = None
        self._sandbox_executor: SandboxExecutor | None = None

        # Voice handler (populated by _init_voice)
        self._voice_routes: Any = None

    # -- Public API --------------------------------------------------------

    async def build(self) -> web.Application:
        """Wire everything together and return the application."""
        self._mode = cfg.server_mode
        cfg.ensure_dirs()
        self._ensure_admin_secret()
        await self._init_core()
        self._init_services()
        self._cross_wire()
        self._init_voice()

        middlewares = [lockdown_middleware, tunnel_restriction_middleware, auth_middleware]
        proxy_mw = self._maybe_create_proxy()
        if proxy_mw is not None:
            middlewares.append(proxy_mw)

        app = web.Application(middlewares=middlewares)
        app["voice_configured"] = self._voice_routes is not None
        app["server_mode"] = self._mode.value

        self._register_routes(app)
        self._register_lifecycle(app)

        if proxy_mw is not None:
            app.on_cleanup.append(proxy_mw.cleanup)

        return app

    # -- Properties --------------------------------------------------------

    @property
    def _is_admin(self) -> bool:
        return self._mode in (ServerMode.admin, ServerMode.combined)

    @property
    def _is_runtime(self) -> bool:
        return self._mode in (ServerMode.runtime, ServerMode.combined)

    # -- Initialisation (delegates to wiring module) -----------------------

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
        core = await init_core(self._mode)
        self._agent = core["agent"]
        self._adapter = core["adapter"]
        self._conv_store = core["conv_store"]
        self._session_store = core["session_store"]
        self._bot = core["bot"]
        self._bot_ep = core["bot_ep"]

    def _init_services(self) -> None:
        svc = init_services(self._mode)
        self._tunnel = svc["tunnel"]
        self._deploy_store = svc["deploy_store"]
        self._infra_store = svc["infra_store"]
        self._mcp_store = svc["mcp_store"]
        self._sandbox_store = svc["sandbox_store"]
        self._foundry_iq_store = svc["foundry_iq_store"]
        self._guardrails_store = svc["guardrails_store"]
        self._monitoring_store = svc["monitoring_store"]
        self._az = svc["az"]
        self._gh = svc["gh"]
        self._deployer = svc["deployer"]
        self._provisioner = svc["provisioner"]
        self._aca_deployer = svc["aca_deployer"]
        self._scheduler = svc["scheduler"]
        self._proactive_store = svc["proactive_store"]
        self._sandbox_executor = svc["sandbox_executor"]

        # Wire sandbox and guardrails into agent
        if self._is_runtime and self._agent:
            if self._sandbox_executor:
                self._agent.set_sandbox(self._sandbox_executor)
            self._agent.set_guardrails(self._guardrails_store)

    def _cross_wire(self) -> None:
        """Wire cross-cutting references that span core and services."""
        if self._bot and self._agent and self._agent.hitl_interceptor:
            self._bot._hitl = self._agent.hitl_interceptor
            self._bot._processor._hitl = self._agent.hitl_interceptor

        if self._scheduler and self._agent and self._agent.hitl_interceptor:
            self._scheduler.set_hitl_interceptor(self._agent.hitl_interceptor)
        if self._bot and self._scheduler:
            self._bot._scheduler = self._scheduler

    def _init_voice(self) -> None:
        self._voice_routes = None
        if self._is_runtime:
            self._voice_routes = create_voice_handler(self._agent, self._tunnel)

    def _maybe_create_proxy(self) -> object | None:
        """Create the runtime proxy middleware for admin-only mode."""
        if not (self._is_admin and not self._is_runtime):
            return None
        from .runtime_proxy import create_runtime_proxy_middleware

        if os.getenv("POLYCLAW_USE_MI"):
            aca_fqdn = cfg.env.read("ACA_RUNTIME_FQDN")
            if aca_fqdn:
                aca_url = f"https://{aca_fqdn}"
                os.environ["RUNTIME_URL"] = aca_url
                logger.info("[startup] Restored RUNTIME_URL=%s from ACA deployment", aca_url)

        return create_runtime_proxy_middleware()

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
        router.add_get("/api/media/{filename:.+}", serve_media)
        router.add_get("/health", self._health_handler())

        # Frontend SPA -- served by admin in split mode, or by combined
        if self._is_admin:
            self._register_frontend(router)

    def _register_admin_routes(self, router: web.UrlDispatcher) -> None:
        """Routes available only in ``admin`` or ``combined`` mode."""
        register_admin_routes(
            router,
            az=self._az, gh=self._gh, tunnel=self._tunnel,
            deployer=self._deployer, rebuild_adapter=self._rebuild_adapter,
            infra_store=self._infra_store, provisioner=self._provisioner,
            deploy_store=self._deploy_store, aca_deployer=self._aca_deployer,
            sandbox_store=self._sandbox_store,
            sandbox_executor=self._sandbox_executor,
            foundry_iq_store=self._foundry_iq_store,
            monitoring_store=self._monitoring_store,
            guardrails_store=self._guardrails_store,
        )

    def _register_runtime_routes(self, app: web.Application) -> None:
        """Routes available only in ``runtime`` or ``combined`` mode."""
        register_runtime_routes(
            app,
            agent=self._agent, session_store=self._session_store,
            sandbox_executor=self._sandbox_executor,
            mcp_store=self._mcp_store, guardrails_store=self._guardrails_store,
            scheduler=self._scheduler, proactive_store=self._proactive_store,
            adapter=self._adapter, conv_store=self._conv_store,
            bot_ep=self._bot_ep, tunnel=self._tunnel,
            voice_routes=self._voice_routes,
            handle_reload=self._handle_reload,
        )

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
        fe = FRONTEND_DIR
        if not fe.exists():
            return
        router.add_get("/", serve_index)
        if (fe / "assets").is_dir():
            router.add_static("/assets/", path=str(fe / "assets"), name="fe_assets")
        for fname in ("favicon.ico", "logo.png", "headertext.png"):
            fpath = fe / fname
            if fpath.exists():
                router.add_get(f"/{fname}", make_file_handler(fpath))
        router.add_get("/{tail:[^/].*}", serve_spa_or_404)

    # -- Lifecycle (delegates to lifecycle module) --------------------------

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
            await lifecycle.on_startup_runtime(
                app,
                mode=self._mode,
                adapter=self._adapter,
                bot=self._bot,
                bot_ep=self._bot_ep,
                conv_store=self._conv_store,
                agent=self._agent,
                tunnel=self._tunnel,
                infra_store=self._infra_store,
                provisioner=self._provisioner,
                az=self._az,
                monitoring_store=self._monitoring_store,
                session_store=self._session_store,
                foundry_iq_store=self._foundry_iq_store,
                scheduler=self._scheduler,
                rebuild_adapter=self._rebuild_adapter,
                make_notify=self._make_notify,
            )
        if self._is_admin:
            await lifecycle.on_startup_admin(
                app,
                az=self._az,
                deploy_store=self._deploy_store,
                guardrails_store=self._guardrails_store,
            )

    async def _on_cleanup(self, app: web.Application) -> None:
        await lifecycle.on_cleanup(
            app,
            mode=self._mode,
            infra_store=self._infra_store,
            provisioner=self._provisioner,
            agent=self._agent,
        )

    async def _handle_reload(self, request: web.Request) -> web.Response:
        logger.info("[reload] triggered by admin -- re-reading configuration")

        # 1. Re-read .env from shared volume
        cfg.reload()

        # 2. Reload infra config
        if self._infra_store:
            self._infra_store._load()

        # 3. Reload agent auth
        auth_result: dict = {}
        if self._agent:
            auth_result = await self._agent.reload_auth()
            logger.info("[reload] agent auth: %s", auth_result.get("status"))

        # 4. Rebuild Bot Framework adapter
        self._rebuild_adapter()

        # 5. Reinitialise voice handler
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
            coro = self._pick_bot_reload_coro()
            if coro is not None:
                request.app["reload_bot_task"] = asyncio.create_task(coro)
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

    def _pick_bot_reload_coro(self) -> Any:
        """Return the appropriate bot-reload coroutine, or ``None``."""
        bot_endpoint = os.environ.get("BOT_ENDPOINT", "")
        tunnel_active = (
            getattr(self._tunnel, "is_active", False)
            if self._tunnel
            else False
        )

        if bot_endpoint:
            return lifecycle.recreate_bot(
                provisioner=self._provisioner, az=self._az,
                infra_store=self._infra_store, tunnel=self._tunnel,
                rebuild_adapter=self._rebuild_adapter,
                endpoint_override=bot_endpoint,
            )

        if self._tunnel and not tunnel_active:
            from ..services.deployment.deployer import BotDeployer as _BD

            if _BD._env("BOT_APP_ID"):
                return lifecycle.start_tunnel_and_create_bot(
                    tunnel=self._tunnel,
                    provisioner=self._provisioner,
                    az=self._az,
                    infra_store=self._infra_store,
                    rebuild_adapter=self._rebuild_adapter,
                )

        if self._tunnel and tunnel_active:
            return lifecycle.recreate_bot(
                provisioner=self._provisioner, az=self._az,
                infra_store=self._infra_store, tunnel=self._tunnel,
                rebuild_adapter=self._rebuild_adapter,
            )

        return None



# -- CLI entry point -------------------------------------------------------


def main() -> None:
    """Launch the server from the command line."""
    import argparse

    from .middleware import QuietAccessLogger

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
        os.environ["POLYCLAW_SERVER_MODE"] = "admin"
    elif args.runtime_only:
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

    display_secret = (
        cfg.admin_secret
        if cfg.admin_secret and not cfg.admin_secret.startswith("@kv:")
        else ""
    )
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
