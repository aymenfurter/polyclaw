"""Setup API routes -- /api/setup/*."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable

import aiohttp as _aiohttp
from aiohttp import web

from ...config.settings import SECRET_ENV_KEYS, ServerMode, cfg
from ...services.cloud.azure import AzureCLI
from ...services.cloud.github import GitHubAuth
from ...services.deployment.aca_deployer import AcaDeployer
from ...services.deployment.deployer import BotDeployer
from ...services.deployment.provisioner import Provisioner
from ...state.deploy_state import DeployStateStore
from ...state.infra_config import InfraConfigStore
from ...util.async_helpers import run_sync
from .azure import AzureSetupRoutes
from ._helpers import error_response as _error, ok_response as _ok
from .deploy import DeploymentRoutes
from .foundry import FoundryDeployRoutes
from .preflight import PreflightRoutes
from .prerequisites import PrerequisitesRoutes
from .voice import VoiceSetupRoutes
from ..smoke_test import SmokeTestRunner

logger = logging.getLogger(__name__)


class SetupRoutes:
    """All /api/setup/* route handlers."""

    def __init__(
        self,
        az: AzureCLI,
        gh: GitHubAuth,
        tunnel: object | None,
        deployer: BotDeployer,
        rebuild_adapter: Callable,
        infra_store: InfraConfigStore,
        provisioner: Provisioner,
        deploy_store: DeployStateStore | None = None,
        aca_deployer: AcaDeployer | None = None,
    ) -> None:
        self._az = az
        self._gh = gh
        self._tunnel = tunnel
        self._deployer = deployer
        self._rebuild = rebuild_adapter
        self._store = infra_store
        self._provisioner = provisioner
        self._deploy_store = deploy_store
        self._aca_deployer = aca_deployer
        self._azure_routes = AzureSetupRoutes(az)
        self._voice_routes = VoiceSetupRoutes(az, infra_store)
        self._prerequisites_routes = PrerequisitesRoutes(az, infra_store, deploy_store)
        self._preflight_routes = PreflightRoutes(tunnel, infra_store, az=az)
        self._foundry_routes = FoundryDeployRoutes(
            az=az,
            deploy_store=deploy_store,
            restart_runtime=self._restart_runtime,
        )
        self._deployment_routes = DeploymentRoutes(
            az=az,
            provisioner=provisioner,
            rebuild_adapter=rebuild_adapter,
            restart_runtime=self._restart_runtime,
            infra_store=infra_store,
            deploy_store=deploy_store,
            aca_deployer=aca_deployer,
        )

    def register(self, router: web.UrlDispatcher) -> None:
        r = router
        r.add_get("/api/setup/status", self.status)
        self._azure_routes.register(r)
        r.add_get("/api/setup/copilot/status", self.copilot_status)
        r.add_post("/api/setup/copilot/login", self.copilot_login)
        r.add_post("/api/setup/copilot/token", self.copilot_set_token)
        r.add_post("/api/setup/copilot/smoke-test", self.smoke_test)
        r.add_post("/api/setup/tunnel/start", self.start_tunnel)
        r.add_post("/api/setup/tunnel/stop", self.stop_tunnel)
        r.add_post("/api/setup/tunnel/restrict", self.toggle_tunnel_restriction)
        r.add_get("/api/setup/bot/config", self.get_bot_config)
        r.add_post("/api/setup/bot/config", self.save_bot_config)
        r.add_get("/api/setup/channels/config", self.get_channels_config)
        r.add_post("/api/setup/channels/telegram/config", self.save_telegram_config)
        r.add_post("/api/setup/channels/telegram/remove", self.remove_telegram_config)
        r.add_post("/api/setup/configuration/save", self.save_configuration)
        self._prerequisites_routes.register(r)
        self._voice_routes.register(r)
        r.add_get("/api/setup/config", self.get_config)
        r.add_post("/api/setup/config", self.save_config)
        self._preflight_routes.register(r)
        self._foundry_routes.register(r)
        self._deployment_routes.register(r)

    # -- Status --

    async def status(self, _req: web.Request) -> web.Response:
        from ..tunnel_status import resolve_tunnel_info

        account = self._az.account_info()
        kv_url = cfg.env.read("KEY_VAULT_URL") or ""
        tunnel_info = await resolve_tunnel_info(self._tunnel, self._az)

        logged_in = account is not None
        needs_subscription = bool(account and account.get("_no_default_subscription"))

        return web.json_response({
            "azure": {
                "logged_in": logged_in,
                "needs_subscription": needs_subscription,
                "user": account.get("user", {}).get("name") if account and not needs_subscription else None,
                "subscription": account.get("name") if account and not needs_subscription else None,
                "subscription_id": account.get("id") if account and not needs_subscription else None,
            },
            "foundry": {
                "deployed": bool(cfg.foundry_endpoint),
                "endpoint": cfg.foundry_endpoint,
                "name": cfg.foundry_name,
                "resource_group": cfg.foundry_resource_group,
            },
            "tunnel": tunnel_info,
            "lockdown_mode": cfg.lockdown_mode,
            "prerequisites_configured": bool(kv_url),
            "bot_configured": self._store.bot_configured,
            "bot_deployed": bool(cfg.env.read("BOT_NAME")),
            "telegram_configured": self._store.telegram_configured,
            "voice_call_configured": self._store.voice_call_configured,
            "model": cfg.copilot_model,
            "env_path": str(cfg.env.path),
            "data_dir": str(cfg.data_dir),
        })

    # -- Copilot --

    async def copilot_status(self, _req: web.Request) -> web.Response:
        info = self._gh.status()
        return web.json_response(info)

    async def copilot_login(self, _req: web.Request) -> web.Response:
        status, info = self._gh.start_login()
        return web.json_response(
            {"status": status, **info}, status=500 if status == "error" else 200
        )

    async def copilot_set_token(self, req: web.Request) -> web.Response:
        return _error("GitHub token is no longer used. Configure FOUNDRY_ENDPOINT instead.", 410)

    async def _restart_runtime(self) -> None:
        """Restart or reload the runtime container.

        Docker mode:  full ``docker restart`` so the entrypoint re-runs
        ``az login --service-principal`` with the SP credentials from
        ``/data/.env``.  A soft reload cannot replicate this.

        Non-Docker:  HTTP POST to ``/api/internal/reload``.
        """
        runtime_url = os.getenv("RUNTIME_URL", "")
        if not runtime_url or cfg.server_mode == ServerMode.combined:
            return

        # Docker mode -- hard restart (re-runs entrypoint + az login)
        # Falls back to ``docker compose up -d runtime`` when the container
        # does not exist yet (first provision).
        if os.getenv("POLYCLAW_CONTAINER") == "1":
            try:
                proc = await run_sync(
                    subprocess.run,
                    ["docker", "restart", "polyclaw-runtime"],
                    capture_output=True, text=True, timeout=60,
                )
                if proc.returncode == 0:
                    logger.info("[setup.restart_runtime] docker restart succeeded")
                else:
                    stderr = proc.stderr.strip()
                    logger.warning(
                        "[setup.restart_runtime] docker restart failed: %s", stderr,
                    )
                    # Container doesn't exist yet -- start it via compose
                    if "No such container" in stderr or "not found" in stderr.lower():
                        logger.info(
                            "[setup.restart_runtime] container missing, "
                            "attempting docker compose up -d runtime",
                        )
                        up = await run_sync(
                            subprocess.run,
                            ["docker", "compose", "up", "-d", "runtime"],
                            capture_output=True, text=True, timeout=120,
                        )
                        if up.returncode == 0:
                            logger.info(
                                "[setup.restart_runtime] compose up succeeded",
                            )
                        else:
                            logger.warning(
                                "[setup.restart_runtime] compose up failed: %s",
                                up.stderr.strip(),
                            )
            except Exception as exc:
                logger.warning(
                    "[setup.restart_runtime] docker restart error: %s",
                    exc, exc_info=True,
                )
            return

        url = f"{runtime_url.rstrip('/')}/api/internal/reload"
        headers: dict[str, str] = {}
        if cfg.admin_secret:
            headers["Authorization"] = f"Bearer {cfg.admin_secret}"

        try:
            async with _aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers,
                    timeout=_aiohttp.ClientTimeout(total=15),
                ) as resp:
                    body = await resp.json()
                    logger.info(
                        "[setup.restart_runtime] reload response: status=%s body=%r",
                        resp.status, body,
                    )
        except Exception as exc:
            logger.warning(
                "[setup.restart_runtime] failed to signal runtime reload: %s",
                exc, exc_info=True,
            )

    async def smoke_test(self, _req: web.Request) -> web.Response:
        runner = SmokeTestRunner(self._gh)
        result = await runner.run()
        return web.json_response(result, status=200 if result["status"] == "ok" else 500)

    # -- Tunnel --

    async def start_tunnel(self, req: web.Request) -> web.Response:
        if not self._tunnel:
            return _error("Tunnel is managed by the runtime container", 400)
        body = await req.json()
        port = body.get("port", cfg.admin_port)
        result = self._tunnel.start(port)
        if not result:
            return _error(result.message)
        url = result.value

        endpoint_updated = False
        if cfg.env.read("BOT_NAME"):
            endpoint = url.rstrip("/") + "/api/messages"
            ok, _ = await run_sync(self._az.update_endpoint, endpoint)
            endpoint_updated = ok
            if ok:
                self._rebuild()

        return web.json_response({
            "status": "ok",
            "url": url,
            "message": result.message,
            "endpoint_updated": endpoint_updated,
        })

    async def stop_tunnel(self, _req: web.Request) -> web.Response:
        if not self._tunnel:
            return _error("Tunnel is managed by the runtime container", 400)
        result = self._tunnel.stop()
        if not result:
            return _error(result.message)
        return web.json_response({"status": "ok", "message": result.message})

    async def toggle_tunnel_restriction(self, req: web.Request) -> web.Response:
        body = await req.json()
        restricted = bool(body.get("restricted", False))

        cfg.write_env(TUNNEL_RESTRICTED="1" if restricted else "")
        state = "enabled" if restricted else "disabled"
        logger.info("Tunnel restriction %s", state)

        deploy_mode = "local"
        if os.getenv("POLYCLAW_USE_MI"):
            deploy_mode = "aca"
        elif os.getenv("POLYCLAW_CONTAINER") == "1":
            deploy_mode = "docker"

        needs_redeploy = deploy_mode in ("aca", "docker")

        return web.json_response({
            "status": "ok",
            "restricted": restricted,
            "message": f"Tunnel restriction {state}",
            "needs_redeploy": needs_redeploy,
            "deploy_mode": deploy_mode,
        })

    # -- Bot config --

    async def get_bot_config(self, _req: web.Request) -> web.Response:
        from dataclasses import asdict
        return web.json_response(asdict(self._store.bot))

    async def save_bot_config(self, req: web.Request) -> web.Response:
        body = await req.json()
        self._store.save_bot(
            resource_group=body.get("resource_group", "polyclaw-rg"),
            location=body.get("location", "eastus"),
            display_name=body.get("display_name", "polyclaw"),
            bot_handle=body.get("bot_handle", ""),
        )
        return _ok("Bot configuration saved")

    # -- Channel config --

    async def get_channels_config(self, _req: web.Request) -> web.Response:
        safe = self._store.to_safe_dict()
        return web.json_response(safe.get("channels", {}))

    async def save_telegram_config(self, req: web.Request) -> web.Response:
        body = await req.json()
        token = body.get("token", "").strip()
        whitelist = body.get("whitelist", "").strip()
        if not token:
            return _error("Telegram bot token is required", 400)

        tok_ok, tok_detail = self._az.validate_telegram_token(token)
        if not tok_ok:
            return _error(f"Invalid Telegram token: {tok_detail}", 400)

        self._store.save_telegram(token=token, whitelist=whitelist)
        return web.json_response({
            "status": "ok", "message": f"Telegram config saved ({tok_detail})"
        })

    async def remove_telegram_config(self, _req: web.Request) -> web.Response:
        self._store.clear_telegram()
        return _ok("Telegram configuration removed")

    # -- Combined save --

    async def save_configuration(self, req: web.Request) -> web.Response:
        body = await req.json()
        steps: list[dict] = []

        tg = body.get("telegram", {})
        tg_token = tg.get("token", "").strip()
        tg_whitelist = tg.get("whitelist", "").strip()

        if tg_token:
            tok_ok, tok_detail = self._az.validate_telegram_token(tg_token)
            if not tok_ok:
                return _error(f"Invalid Telegram token: {tok_detail}", 400)
            steps.append({
                "step": "validate_token", "status": "ok", "detail": tok_detail
            })

        bot = body.get("bot", {})
        self._store.save_bot(
            resource_group=bot.get("resource_group", "polyclaw-rg"),
            location=bot.get("location", "eastus"),
            display_name=bot.get("display_name", "polyclaw"),
            bot_handle=bot.get("bot_handle", ""),
        )
        steps.append({
            "step": "bot_config", "status": "ok", "detail": "Saved"
        })

        kv_steps = await self._prerequisites_routes.ensure_keyvault_ready(
            location=bot.get("location", "eastus"),
        )
        steps.extend(kv_steps)

        kv_failed = any(s.get("status") == "failed" for s in kv_steps)
        if kv_failed:
            return web.json_response({
                "status": "error", "steps": steps,
                "message": "Key Vault creation failed",
            }, status=500)

        if tg_token:
            self._store.save_telegram(token=tg_token, whitelist=tg_whitelist)
            steps.append({
                "step": "telegram_config", "status": "ok",
                "detail": "Stored in Key Vault",
            })

        try:
            migrated = self._prerequisites_routes._migrate_existing_secrets()
            if migrated:
                steps.append({
                    "step": "migrate_env", "status": "ok",
                    "detail": f"Migrated {migrated} secret(s)",
                })
        except Exception as exc:
            logger.warning("Post-save migration failed: %s", exc)
            steps.append({
                "step": "migrate_env", "status": "warning",
                "detail": "Some secrets could not be migrated",
            })

        await self._restart_runtime()
        return web.json_response({
            "status": "ok", "steps": steps,
            "message": "Configuration saved securely",
        })

    # -- Runtime config --

    async def get_config(self, _req: web.Request) -> web.Response:
        raw = {
            "COPILOT_MODEL": cfg.env.read("COPILOT_MODEL") or cfg.copilot_model,
            "BOT_PORT": cfg.env.read("BOT_PORT") or str(cfg.bot_port),
        }
        for key in raw:
            if key in SECRET_ENV_KEYS and raw[key]:
                raw[key] = "****"
        return web.json_response(raw)

    _ALLOWED_CONFIG_KEYS: frozenset[str] = frozenset({
        "COPILOT_MODEL",
        "BOT_PORT",
    })

    async def save_config(self, req: web.Request) -> web.Response:
        body = await req.json()
        invalid = set(body) - self._ALLOWED_CONFIG_KEYS
        if invalid:
            return _error(f"Disallowed config keys: {', '.join(sorted(invalid))}", 400)
        cfg.write_env(**body)
        return _ok("Config saved")
