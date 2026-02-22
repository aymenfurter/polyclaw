"""Bot infrastructure deployment."""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from ..config.settings import cfg
from ..state.deploy_state import DeployStateStore
from .azure import AzureCLI

logger = logging.getLogger(__name__)


@dataclass
class DeployRequest:
    resource_group: str = "polyclaw-rg"
    location: str = "eastus"
    display_name: str = "polyclaw"
    bot_handle: str = ""
    endpoint_url: str = ""  # live tunnel URL; avoids reading stale .env


@dataclass
class DeployResult:
    ok: bool
    steps: list[dict[str, Any]]
    app_id: str = ""
    bot_handle: str = ""
    resource_group: str = ""
    error: str = ""


class BotDeployer:
    """Orchestrates the Azure bot deployment pipeline."""

    def __init__(self, az: AzureCLI, deploy_store: DeployStateStore | None = None) -> None:
        self._az = az
        self._deploy_store = deploy_store

    def deploy(self, req: DeployRequest) -> DeployResult:
        existing = cfg.env.read("BOT_NAME")
        if existing:
            logger.info(
                "Bot '%s' already registered -- deleting before fresh deploy",
                existing,
            )
            self.delete()

        handle = req.bot_handle or self._generate_handle()
        steps: list[dict[str, Any]] = []
        app_id = ""
        logger.info(
            "Starting bot deployment: rg=%s, location=%s, handle=%s",
            req.resource_group, req.location, handle,
        )

        try:
            logger.info("Step 1/4: Creating resource group '%s' in '%s'...", req.resource_group, req.location)
            if not self._create_resource_group(req.resource_group, req.location, steps):
                return DeployResult(ok=False, steps=steps, error=f"Resource group failed: {self._az.last_stderr}")

            logger.info("Step 2/4: Registering app '%s'...", req.display_name)
            app_id = self._register_app(req.display_name, steps)
            if not app_id:
                return DeployResult(ok=False, steps=steps, error=f"App registration failed: {self._az.last_stderr}")

            logger.info("Step 3/4: Creating credentials for app %s...", app_id)
            password, tenant_id = self._create_credentials(app_id, steps)
            if not password:
                return DeployResult(ok=False, steps=steps, error=f"Credential reset failed: {self._az.last_stderr}")

            logger.info("Step 4/4: Creating bot resource '%s'...", handle)
            actual_handle = self._create_bot_resource(
                req.resource_group, handle, app_id, tenant_id, steps,
                endpoint_url=req.endpoint_url,
            )
            if not actual_handle:
                return DeployResult(ok=False, steps=steps, error=f"Bot creation failed: {self._az.last_stderr}")
            handle = actual_handle

            cfg.write_env(
                BOT_APP_ID=app_id, BOT_APP_PASSWORD=password, BOT_APP_TENANT_ID=tenant_id,
                BOT_RESOURCE_GROUP=req.resource_group, BOT_NAME=handle,
            )

            if self._deploy_store:
                rec = self._deploy_store.current_local()
                if rec:
                    rec.add_resource(
                        resource_type="bot", resource_group=req.resource_group,
                        resource_name=handle, purpose="Bot Framework registration",
                    )
                    rec.add_resource(
                        resource_type="app_registration", resource_group=req.resource_group,
                        resource_name=app_id, purpose="Entra ID app registration",
                    )
                    self._deploy_store.update(rec)

            logger.info("Bot deployment completed: handle=%s, app_id=%s", handle, app_id)
            return DeployResult(
                ok=True, steps=steps, app_id=app_id,
                bot_handle=handle, resource_group=req.resource_group,
            )
        except Exception:
            if app_id:
                logger.info("Cleaning up orphaned app registration %s", app_id)
                self._az.ok("ad", "app", "delete", "--id", app_id)
            raise

    def register_app(self, req: DeployRequest) -> DeployResult:
        """Create Entra ID app registration and credentials only.

        The admin container calls this to prepare the bot identity.  The
        Bot Service ARM resource is created later by the agent container
        at startup (see :meth:`recreate`).
        """
        steps: list[dict[str, Any]] = []
        app_id = ""
        logger.info(
            "Registering Entra app: rg=%s, location=%s, display_name=%s",
            req.resource_group, req.location, req.display_name,
        )

        try:
            logger.info("Step 1/3: Creating resource group '%s' in '%s'...",
                         req.resource_group, req.location)
            if not self._create_resource_group(req.resource_group, req.location, steps):
                return DeployResult(
                    ok=False, steps=steps,
                    error=f"Resource group failed: {self._az.last_stderr}",
                )

            logger.info("Step 2/3: Registering app '%s'...", req.display_name)
            app_id = self._register_app(req.display_name, steps)
            if not app_id:
                return DeployResult(
                    ok=False, steps=steps,
                    error=f"App registration failed: {self._az.last_stderr}",
                )

            logger.info("Step 3/3: Creating credentials for app %s...", app_id)
            password, tenant_id = self._create_credentials(app_id, steps)
            if not password:
                return DeployResult(
                    ok=False, steps=steps,
                    error=f"Credential reset failed: {self._az.last_stderr}",
                )

            cfg.write_env(
                BOT_APP_ID=app_id, BOT_APP_PASSWORD=password,
                BOT_APP_TENANT_ID=tenant_id,
                BOT_RESOURCE_GROUP=req.resource_group,
            )

            if self._deploy_store:
                rec = self._deploy_store.current_local()
                if rec:
                    rec.add_resource(
                        resource_type="app_registration",
                        resource_group=req.resource_group,
                        resource_name=app_id,
                        purpose="Entra ID app registration",
                    )
                    self._deploy_store.update(rec)

            logger.info("App registration completed: app_id=%s", app_id)
            return DeployResult(
                ok=True, steps=steps, app_id=app_id,
                resource_group=req.resource_group,
            )
        except Exception:
            if app_id:
                logger.info("Cleaning up orphaned app registration %s", app_id)
                self._az.ok("ad", "app", "delete", "--id", app_id)
            raise

    def delete(self) -> DeployResult:
        rg = cfg.env.read("BOT_RESOURCE_GROUP")
        name = cfg.env.read("BOT_NAME")
        app_id = cfg.env.read("BOT_APP_ID")
        if not name:
            return DeployResult(ok=False, steps=[], error="No bot configured")

        steps: list[dict[str, Any]] = []
        logger.info("Deleting bot: name=%s, rg=%s, app_id=%s", name, rg, app_id)

        ok, _ = self._az.ok("bot", "delete", "--resource-group", rg, "--name", name)
        steps.append({"step": "bot_resource", "status": "ok" if ok else "failed"})

        if app_id:
            ok2, _ = self._az.ok("ad", "app", "delete", "--id", app_id)
            steps.append({"step": "app_registration", "status": "ok" if ok2 else "failed"})

        cfg.write_env(
            BOT_APP_ID="", BOT_APP_PASSWORD="", BOT_APP_TENANT_ID="",
            BOT_RESOURCE_GROUP="", BOT_NAME="",
        )
        return DeployResult(ok=True, steps=steps)

    @staticmethod
    def _env(key: str) -> str:
        """Read a value from .env file, falling back to ``os.environ``."""
        return cfg.env.read(key) or os.getenv(key, "")

    def recreate(self, endpoint_url: str) -> DeployResult:
        """Delete and recreate the bot resource with a new endpoint.

        Unlike :meth:`deploy` / :meth:`delete`, this preserves the existing
        Entra ID app registration and credentials.  It only touches the Bot
        Service ARM resource, which requires ``Bot Service Contributor``
        (no Graph API permissions needed).
        """
        rg = self._env("BOT_RESOURCE_GROUP")
        name = self._env("BOT_NAME")
        app_id = self._env("BOT_APP_ID")
        tenant_id = self._env("BOT_APP_TENANT_ID")

        logger.info(
            "recreate: rg=%s name=%s app_id=%s tenant_id=%s",
            rg or "(empty)", name or "(empty)",
            app_id[:12] + "..." if app_id else "(empty)",
            tenant_id[:12] + "..." if tenant_id else "(empty)",
        )

        if not (rg and app_id and tenant_id):
            missing = [k for k, v in [
                ("BOT_RESOURCE_GROUP", rg),
                ("BOT_APP_ID", app_id),
                ("BOT_APP_TENANT_ID", tenant_id),
            ] if not v]
            return DeployResult(
                ok=False, steps=[],
                error=(
                    f"Missing {', '.join(missing)} -- the admin container must "
                    "provision the bot first (Setup Wizard > Infrastructure)"
                ),
            )

        steps: list[dict[str, Any]] = []

        # 1. Delete the existing bot resource (not the app registration)
        if name:
            logger.info("Recreate: deleting bot resource %s in %s", name, rg)
            ok, _ = self._az.ok("bot", "delete", "--resource-group", rg, "--name", name)
            steps.append({
                "step": "bot_delete",
                "status": "ok" if ok else "warn",
                "detail": name if ok else self._az.last_stderr,
            })

        # 2. Create a new bot resource with the same app credentials
        handle = name or self._generate_handle()
        logger.info(
            "Recreate: creating bot %s in %s with endpoint %s",
            handle, rg, endpoint_url,
        )
        actual = self._create_bot_resource(
            rg, handle, app_id, tenant_id, steps,
            endpoint_url=endpoint_url,
        )
        if not actual:
            return DeployResult(
                ok=False, steps=steps,
                error=f"Bot creation failed: {self._az.last_stderr}",
            )

        # Persist the (possibly new) handle
        cfg.write_env(BOT_NAME=actual)
        steps.append({"step": "bot_recreate", "status": "ok", "detail": actual})

        logger.info("Recreate completed: handle=%s, endpoint=%s", actual, endpoint_url)
        return DeployResult(
            ok=True, steps=steps, app_id=app_id,
            bot_handle=actual, resource_group=rg,
        )

    def _create_resource_group(self, name: str, location: str, steps: list[dict]) -> bool:
        tag_args: list[str] = []
        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                tag_args = ["--tags", f"polyclaw_deploy={rec.tag}"]
        result = self._az.json("group", "create", "--name", name, "--location", location, *tag_args)
        if result:
            steps.append({"step": "resource_group", "status": "ok", "name": name})
            if self._deploy_store:
                rec = self._deploy_store.current_local()
                if rec and name not in rec.resource_groups:
                    rec.resource_groups.append(name)
                    self._deploy_store.update(rec)
        return bool(result)

    def _register_app(self, display_name: str, steps: list[dict]) -> str:
        # Check for an existing app registration with the same display name
        existing_list = self._az.json(
            "ad", "app", "list",
            "--display-name", display_name,
        )
        existing = existing_list[0] if isinstance(existing_list, list) and existing_list else None
        if isinstance(existing, dict) and existing.get("appId"):
            app_id = existing["appId"]
            logger.info("Reusing existing app registration: %s (appId=%s)", display_name, app_id)
            steps.append({"step": "app_registration", "status": "ok", "app_id": app_id, "reused": True})
            return app_id

        app = self._az.json(
            "ad", "app", "create", "--display-name", display_name,
            "--sign-in-audience", "AzureADMyOrg",
        )
        if not isinstance(app, dict):
            return ""
        app_id = app.get("appId", "")
        sp = self._az.json("ad", "sp", "create", "--id", app_id)
        if not sp and "already in use" in (self._az.last_stderr or ""):
            logger.info("Service principal already exists for %s -- continuing", app_id)
        steps.append({"step": "app_registration", "status": "ok", "app_id": app_id})
        return app_id

    def _create_credentials(self, app_id: str, steps: list[dict]) -> tuple[str, str]:
        cred = self._az.json("ad", "app", "credential", "reset", "--id", app_id, "--years", "2")
        if not isinstance(cred, dict):
            return "", ""
        steps.append({"step": "client_secret", "status": "ok"})
        return cred.get("password", ""), cred.get("tenant", "")

    def _create_bot_resource(
        self, rg: str, handle: str, app_id: str, tenant_id: str, steps: list[dict],
        endpoint_url: str = "",
    ) -> str | None:
        """Create or reuse a bot resource. Returns actual handle or None on failure."""
        # Use the explicitly-passed URL from the live tunnel.
        # TUNNEL_URL is never persisted -- it is a runtime-only value.
        endpoint_args: list[str] = []
        if endpoint_url:
            endpoint_args = ["--endpoint", endpoint_url.rstrip("/") + "/api/messages"]

        # Check if a bot already exists in this resource group
        bot_list = self._az.json(
            "resource", "list", "--resource-group", rg,
            "--resource-type", "Microsoft.BotService/botServices",
        )
        existing_bots = bot_list[0] if isinstance(bot_list, list) and bot_list else None
        if isinstance(existing_bots, dict) and existing_bots.get("name"):
            actual_name = existing_bots["name"]
            logger.info(
                "Bot resource already exists: %s -- deleting before recreate", actual_name
            )
            self._az.ok("bot", "delete", "--resource-group", rg, "--name", actual_name)
            steps.append({"step": "bot_resource_cleanup", "status": "ok", "name": actual_name})

        bot = self._az.json(
            "bot", "create", "--resource-group", rg, "--name", handle,
            "--app-type", "SingleTenant", "--appid", app_id,
            "--tenant-id", tenant_id, "--sku", "F0", *endpoint_args,
        )
        if not bot and "already in use" in (self._az.last_stderr or ""):
            logger.info("Bot resource already exists for app %s -- continuing", app_id)
            bot = True
        if bot:
            steps.append({"step": "bot_resource", "status": "ok", "name": handle})
            return handle
        return None

    @staticmethod
    def _generate_handle() -> str:
        ts = hex(int(time.time()) % 0xFFFF)[2:]
        return f"polyclaw-{secrets.token_hex(4)}-{ts}"
