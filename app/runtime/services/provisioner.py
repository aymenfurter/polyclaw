"""Infrastructure provisioner -- reconcile Azure state with config."""

from __future__ import annotations

import logging
from typing import Any

from ..config.settings import cfg
from ..state.deploy_state import DeploymentRecord, DeployStateStore
from ..state.infra_config import InfraConfigStore
from .azure import AzureCLI
from .deployer import BotDeployer, DeployRequest
from .runtime_identity import RuntimeIdentityProvisioner

logger = logging.getLogger(__name__)


class Provisioner:
    """Orchestrates full infrastructure lifecycle from config."""

    def __init__(
        self,
        az: AzureCLI,
        deployer: BotDeployer,
        store: InfraConfigStore,
        deploy_store: DeployStateStore | None = None,
        *,
        tunnel: object | None = None,
    ) -> None:
        self._az = az
        self._deployer = deployer
        self._tunnel = tunnel
        self._store = store
        self._deploy_store = deploy_store
        self._runtime_identity = RuntimeIdentityProvisioner(az)

    def provision(self) -> list[dict[str, Any]]:
        """Register Entra ID app + provision a scoped agent identity.

        The Bot Service ARM resource is NOT created here -- the agent
        container creates it at startup when a messaging channel (e.g.
        Telegram) is configured.  See :meth:`recreate_endpoint`.
        """
        steps: list[dict[str, Any]] = []
        bc = self._store.bot
        logger.info("Provisioning started")

        if not self._store.bot_configured:
            logger.info("No bot configured -- skipping provisioning")
            steps.append({"step": "bot_config", "status": "skip", "detail": "No bot configured"})
            return steps

        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if not rec:
                rec = DeploymentRecord.new(kind="local")
                self._deploy_store.register(rec)
                logger.info("Created new local deployment record: %s", rec.deploy_id)

        # Step 1: Register Entra ID app (no Bot Service resource -- the agent
        # container creates that at startup when a channel is configured).
        logger.info("Provision step 1/2: Registering Entra ID app...")
        if not self._ensure_app_registration(bc, steps):
            logger.error("Provisioning aborted: app registration failed")
            return steps

        # Step 2: Provision scoped identity for the agent container.
        logger.info("Provision step 2/2: Provisioning runtime identity...")
        self._ensure_runtime_identity(bc.resource_group, steps)

        logger.info("Provisioning completed: %d steps", len(steps))
        return steps

    def _ensure_app_registration(
        self, bc: Any, steps: list[dict],
    ) -> bool:
        """Create or re-use the Entra ID app registration (no bot service)."""
        req = DeployRequest(
            resource_group=bc.resource_group,
            location=bc.location,
            display_name=bc.display_name,
            bot_handle=bc.bot_handle,
        )
        result = self._deployer.register_app(req)
        steps.extend(result.steps)
        if result.ok:
            steps.append({
                "step": "app_registration",
                "status": "ok",
                "detail": result.app_id,
            })
        else:
            steps.append({
                "step": "app_registration",
                "status": "failed",
                "detail": result.error,
            })
        return result.ok

    def _ensure_channels(self, steps: list[dict]) -> None:
        tg = self._store.channels.telegram
        if tg.token:
            tok_ok, tok_detail = self._az.validate_telegram_token(tg.token)
            if not tok_ok:
                steps.append({"step": "telegram_validate", "status": "failed", "detail": tok_detail})
                return
            steps.append({"step": "telegram_validate", "status": "ok", "detail": tok_detail})
            # Pass validated_name so configure_telegram skips a redundant API call.
            ok, msg = self._az.configure_telegram(tg.token, validated_name=tok_detail)
            steps.append({"step": "telegram_channel", "status": "ok" if ok else "failed", "detail": msg})
            if ok and tg.whitelist:
                cfg.write_env(TELEGRAM_WHITELIST=tg.whitelist)
        else:
            steps.append({"step": "telegram", "status": "skip", "detail": "Not configured"})

    def _ensure_runtime_identity(self, resource_group: str, steps: list[dict]) -> None:
        """Provision a scoped identity for the agent runtime container.

        Uses a service principal for Docker Compose deployments.  ACA
        deployments use a managed identity provisioned by ``AcaDeployer``
        instead -- this step is skipped if a MI is already configured.
        """
        # Skip if a managed identity is already set (ACA deployment)
        if cfg.env.read("ACA_MI_CLIENT_ID"):
            steps.append({
                "step": "runtime_identity",
                "status": "skip",
                "detail": "Managed identity already configured (ACA)",
            })
            return

        try:
            result = self._runtime_identity.provision(resource_group)
            sub_steps = result.get("steps", [])
            steps.extend(sub_steps)
            if result.get("ok"):
                steps.append({
                    "step": "runtime_identity",
                    "status": "ok",
                    "detail": f"SP {result.get('app_id')} scoped to {resource_group}",
                })
            else:
                steps.append({
                    "step": "runtime_identity",
                    "status": "failed",
                    "detail": result.get("error", "Unknown error"),
                })
        except Exception as exc:
            logger.warning("Runtime identity provisioning failed (non-fatal): %s", exc, exc_info=True)
            steps.append({
                "step": "runtime_identity",
                "status": "failed",
                "detail": str(exc),
            })

    def recreate_endpoint(self, endpoint_url: str) -> list[dict[str, Any]]:
        """Recreate the bot resource with a new messaging endpoint.

        This is the lightweight path for the runtime container: it only
        touches the Bot Service ARM resource and reconfigures channels.
        The Entra ID app registration and credentials are preserved.
        """
        steps: list[dict[str, Any]] = []
        logger.info("recreate_endpoint: endpoint=%s", endpoint_url)

        if not self._store.bot_configured:
            steps.append({"step": "bot_config", "status": "skip",
                          "detail": "No bot configured"})
            return steps

        result = self._deployer.recreate(endpoint_url)
        steps.extend(result.steps)
        if not result.ok:
            logger.error("recreate_endpoint: bot recreate failed: %s", result.error)
            return steps

        # Reconfigure channels (Telegram, etc.) on the fresh bot resource
        self._ensure_channels(steps)

        logger.info("recreate_endpoint: completed -- %d steps", len(steps))
        return steps

    def decommission(self) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        logger.info("Decommissioning started")

        rg = cfg.env.read("BOT_RESOURCE_GROUP")
        name = cfg.env.read("BOT_NAME")
        app_id = cfg.env.read("BOT_APP_ID")
        logger.info(
            "Current state: rg=%s, bot=%s, app_id=%s",
            rg, name, app_id[:12] + "..." if app_id else None,
        )

        if name and rg:
            bot_exists = self._az.json("bot", "show", "--resource-group", rg, "--name", name) is not None
            if bot_exists and self._store.telegram_configured:
                ok, msg = self._az.remove_channel("telegram")
                steps.append({"step": "telegram_remove", "status": "ok" if ok else "failed", "detail": msg})
            elif not bot_exists:
                steps.append({"step": "telegram_remove", "status": "skip", "detail": "Bot resource not found"})

        if name:
            result = self._deployer.delete()
            steps.extend(result.steps)
            steps.append({
                "step": "bot_delete",
                "status": "ok" if result.ok else "failed",
                "detail": "Bot deleted" if result.ok else (result.error or "Failed"),
            })
        elif app_id:
            # Entra app exists but agent hasn't created the bot service yet.
            ok, _ = self._az.ok("ad", "app", "delete", "--id", app_id)
            steps.append({
                "step": "app_delete",
                "status": "ok" if ok else "failed",
                "detail": f"Deleted Entra app {app_id[:12]}..." if ok else "Delete failed",
            })
        else:
            steps.append({"step": "bot_delete", "status": "skip", "detail": "No bot deployed"})

        voice_rg = self._store.channels.voice_call.voice_resource_group or ""
        prereq_rg = cfg.env.read("KEY_VAULT_RG") or ""
        protected_rgs = {rg_name for rg_name in (voice_rg, prereq_rg) if rg_name}

        if rg:
            if rg in protected_rgs:
                reason = []
                if rg == voice_rg:
                    reason.append("voice")
                if rg == prereq_rg:
                    reason.append("prerequisites")
                label = " & ".join(reason)
                logger.info("Skipping RG deletion: %s is the %s resource group", rg, label)
                steps.append({"step": "resource_group_delete", "status": "skip", "detail": f"{rg} is the {label} RG -- not deleting"})
            else:
                rg_exists = self._az.json("group", "show", "--name", rg) is not None
                if rg_exists:
                    ok, msg = self._az.ok("group", "delete", "--name", rg, "--yes", "--no-wait")
                    steps.append({"step": "resource_group_delete", "status": "ok" if ok else "failed", "detail": f"Deleting {rg}" if ok else msg})
                else:
                    steps.append({"step": "resource_group_delete", "status": "skip", "detail": "RG not found"})

        cfg.write_env(
            BOT_APP_ID="", BOT_APP_PASSWORD="", BOT_APP_TENANT_ID="",
            BOT_RESOURCE_GROUP="", BOT_NAME="",
        )

        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.mark_stopped()
                self._deploy_store.update(rec)

        return steps

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "config": self._store.to_safe_dict(),
            "provisioned": {},
            "in_sync": True,
        }

        prov: dict[str, Any] = {}
        prov["tunnel"] = {
            "active": getattr(self._tunnel, "is_active", False),
            "url": getattr(self._tunnel, "url", None),
        }
        if not self._tunnel.is_active:
            result["in_sync"] = False

        bot_name = cfg.env.read("BOT_NAME")
        bot_rg = cfg.env.read("BOT_RESOURCE_GROUP")
        bot_deployed = bool(bot_name)
        prov["bot"] = {
            "deployed": bot_deployed, "name": bot_name or None,
            "resource_group": bot_rg or None,
            "app_id": (cfg.bot_app_id[:12] + "...") if cfg.bot_app_id else None,
        }
        if self._store.bot_configured and not bot_deployed:
            result["in_sync"] = False

        channels: dict[str, Any] = {}
        if self._store.telegram_configured:
            if bot_deployed:
                live_channels = self._az.get_channels()
                tg_live = live_channels.get("telegram", False)
                channels["telegram"] = {"live": tg_live}
                if not tg_live:
                    result["in_sync"] = False
            else:
                channels["telegram"] = {"live": False}
                result["in_sync"] = False
        prov["channels"] = channels

        result["provisioned"] = prov
        return result
