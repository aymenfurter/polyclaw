"""Application lifecycle -- startup and cleanup hooks."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ..config.settings import ServerMode, cfg
from .wiring import create_adapter, create_voice_handler

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_SCHEDULE_INTERVALS = {"hourly": 3600, "daily": 86400}


async def on_startup_runtime(
    app: web.Application,
    *,
    mode: ServerMode,
    adapter: object,
    bot: object | None,
    bot_ep: object | None,
    conv_store: object | None,
    agent: object | None,
    tunnel: object | None,
    infra_store: object,
    provisioner: object | None,
    az: object | None,
    monitoring_store: object,
    session_store: object | None,
    foundry_iq_store: object,
    scheduler: object | None,
    rebuild_adapter: Callable,
    make_notify: Callable[[], Callable[[str], Awaitable[bool]]],
) -> None:
    """Start background tasks and bot infrastructure for the runtime."""
    from ..messaging.proactive_loop import proactive_delivery_loop
    from ..scheduler import scheduler_loop
    from ..services.otel import configure_otel

    # Bootstrap OTel if monitoring is configured
    mon = monitoring_store
    if mon.is_configured:
        configure_otel(
            mon.connection_string,
            sampling_ratio=mon.config.sampling_ratio,
            enable_live_metrics=mon.config.enable_live_metrics,
        )

    rebuild_adapter()

    app["scheduler_task"] = asyncio.create_task(scheduler_loop())
    app["proactive_task"] = asyncio.create_task(
        proactive_delivery_loop(make_notify(), session_store=session_store),
    )
    app["foundry_iq_task"] = asyncio.create_task(
        _foundry_iq_index_loop(foundry_iq_store),
    )

    logger.info(
        "[startup.runtime] mode=%s lockdown=%s bot_configured=%s "
        "telegram_configured=%s tunnel=%s provisioner=%s az=%s",
        mode.value, cfg.lockdown_mode,
        infra_store.bot_configured if infra_store else "<no store>",
        infra_store.telegram_configured if infra_store else "<no store>",
        tunnel is not None,
        provisioner is not None,
        az is not None,
    )

    if cfg.lockdown_mode:
        logger.info("Lock Down Mode active -- skipping infrastructure provisioning")
        return

    bot_endpoint = os.environ.get("BOT_ENDPOINT", "")

    if mode != ServerMode.combined:
        github_token = cfg.github_token
        if not github_token:
            logger.warning(
                "[startup.runtime] Setup incomplete -- missing GITHUB_TOKEN. "
                "Complete the setup wizard in the admin container, "
                "then recreate the agent container.",
            )
            return

    needs_bot = (
        infra_store.bot_configured
        and infra_store.telegram_configured
    )

    if mode == ServerMode.combined:
        if infra_store.bot_configured and provisioner:
            from ..util.async_helpers import run_sync

            logger.info("Startup: provisioning infrastructure from config ...")
            steps = await run_sync(provisioner.provision)
            rebuild_adapter()
            for s in steps:
                logger.info(
                    "  provision: %s = %s (%s)",
                    s.get("step"), s.get("status"), s.get("detail", ""),
                )
        if needs_bot and tunnel:
            await start_tunnel_and_create_bot(
                tunnel=tunnel, provisioner=provisioner, az=az,
                infra_store=infra_store, rebuild_adapter=rebuild_adapter,
            )

    elif bot_endpoint:
        cfg.reload()
        rebuild_adapter()
        if needs_bot:
            logger.info("Static bot endpoint: %s", bot_endpoint)
            await recreate_bot(
                provisioner=provisioner, az=az, infra_store=infra_store,
                tunnel=tunnel, rebuild_adapter=rebuild_adapter,
                endpoint_override=bot_endpoint,
            )
        else:
            logger.info("No messaging channels configured -- skipping bot service")

    else:
        if needs_bot and tunnel:
            from ..services.deployment.deployer import BotDeployer

            bot_app_id = BotDeployer._env("BOT_APP_ID")
            if not bot_app_id:
                logger.warning(
                    "Telegram configured but BOT_APP_ID missing -- "
                    "run Infrastructure Deploy in the admin wizard first"
                )
            else:
                await start_tunnel_and_create_bot(
                    tunnel=tunnel, provisioner=provisioner, az=az,
                    infra_store=infra_store, rebuild_adapter=rebuild_adapter,
                )
        else:
            reasons = []
            if not infra_store.bot_configured:
                reasons.append("bot not configured")
            if not infra_store.telegram_configured:
                reasons.append("no channels configured")
            if not tunnel:
                reasons.append("no tunnel")
            logger.info(
                "Skipping bot service: %s",
                ", ".join(reasons) or "no reason",
            )


async def on_startup_admin(
    app: web.Application,
    *,
    az: object | None,
    deploy_store: object,
    guardrails_store: object,
) -> None:
    """Admin startup: reconcile stale deployments and RBAC."""
    if az:
        app["reconcile_task"] = asyncio.create_task(
            _reconcile_deployments(az, deploy_store),
        )
        app["cs_rbac_task"] = asyncio.create_task(
            _ensure_content_safety_rbac(az, guardrails_store),
        )


async def on_cleanup(
    app: web.Application,
    *,
    mode: ServerMode,
    infra_store: object,
    provisioner: object | None,
    agent: object | None,
) -> None:
    """Cancel background tasks and decommission infrastructure on shutdown."""
    for key in ("scheduler_task", "proactive_task", "foundry_iq_task", "reconcile_task"):
        task = app.get(key)
        if task and not task.done():
            task.cancel()

    if mode == ServerMode.combined:
        if cfg.lockdown_mode:
            logger.info("Lock Down Mode active -- skipping shutdown decommission")
        elif (
            infra_store.bot_configured
            and (cfg.env.read("BOT_NAME") or cfg.env.read("BOT_APP_ID"))
            and provisioner
        ):
            from ..util.async_helpers import run_sync

            logger.info("Shutdown: decommissioning infrastructure ...")
            steps = await run_sync(provisioner.decommission)
            for s in steps:
                logger.info(
                    "  decommission: %s = %s (%s)",
                    s.get("step"), s.get("status"), s.get("detail", ""),
                )

    if agent:
        await agent.stop()


# -- Bot infrastructure helpers -------------------------------------------

async def recreate_bot(
    *,
    provisioner: object | None,
    az: object | None,
    infra_store: object,
    tunnel: object | None,
    rebuild_adapter: Callable,
    endpoint_override: str | None = None,
) -> None:
    """Recreate the bot service endpoint."""
    from ..util.async_helpers import run_sync

    logger.info(
        "[recreate_bot] provisioner=%s az=%s bot_configured=%s endpoint_override=%s",
        provisioner is not None,
        az is not None,
        infra_store.bot_configured if infra_store else "?",
        endpoint_override,
    )
    if not (provisioner and az and infra_store.bot_configured):
        logger.warning(
            "[recreate_bot] precondition failed -- provisioner=%s az=%s bot_configured=%s",
            provisioner is not None,
            az is not None,
            infra_store.bot_configured if infra_store else "?",
        )
        return

    tunnel_url = endpoint_override or getattr(tunnel, "url", None)
    if not tunnel_url:
        logger.warning("Bot recreate: no endpoint URL available -- skipping")
        return

    endpoint = tunnel_url
    logger.info("Bot recreate: endpoint %s", endpoint)
    try:
        steps = await run_sync(provisioner.recreate_endpoint, endpoint)
        rebuild_adapter()
        for s in steps:
            logger.info(
                "  recreate: %s = %s (%s)",
                s.get("step"), s.get("status"), s.get("detail", ""),
            )
    except Exception as exc:
        logger.warning("Bot recreate: error -- %s", exc, exc_info=True)


async def start_tunnel_and_create_bot(
    *,
    tunnel: object,
    provisioner: object | None,
    az: object | None,
    infra_store: object,
    rebuild_adapter: Callable,
) -> None:
    """Start the Cloudflare tunnel and recreate the bot service."""
    from ..util.async_helpers import run_sync

    logger.info("Starting tunnel for bot service endpoint ...")
    tunnel_url = tunnel.url
    if not tunnel_url and not tunnel.is_active:
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            result = await run_sync(tunnel.start, cfg.admin_port)
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

    rebuild_adapter()
    await recreate_bot(
        provisioner=provisioner, az=az, infra_store=infra_store,
        tunnel=tunnel, rebuild_adapter=rebuild_adapter,
    )


# -- Background loops -----------------------------------------------------

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
                logger.info(
                    "Foundry IQ indexing: %s (indexed=%s)",
                    result.get("status"), result.get("indexed", 0),
                )
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


async def _reconcile_deployments(az: object, deploy_store: object) -> None:
    from ..services.resource_tracker import ResourceTracker
    from ..util.async_helpers import run_sync

    try:
        tracker = ResourceTracker(az, deploy_store)
        cleaned = await run_sync(tracker.reconcile)
        if cleaned:
            logger.info(
                "Startup reconcile: removed %d stale deployment(s): %s",
                len(cleaned), ", ".join(c["deploy_id"] for c in cleaned),
            )
    except Exception as exc:
        logger.warning("Startup reconcile failed (non-fatal): %s", exc)


async def _ensure_content_safety_rbac(az: object, guardrails_store: object) -> None:
    from .routes.content_safety_routes import ContentSafetyRoutes

    try:
        routes = ContentSafetyRoutes(
            az=az,
            guardrails_store=guardrails_store,
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
