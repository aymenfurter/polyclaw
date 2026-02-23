"""Monitoring API routes -- /api/monitoring."""

from __future__ import annotations

import logging
import secrets as _secrets
from typing import Any

from aiohttp import web

from ...services.cloud.azure import AzureCLI
from ...services.otel import configure_otel, get_status, is_active, shutdown_otel
from ...state.deploy_state import DeployStateStore
from ...state.monitoring_config import MonitoringConfigStore
from ...util.async_helpers import run_sync
from ._helpers import fail_response as _fail_response, no_az as _no_az

logger = logging.getLogger(__name__)

_DEFAULT_MONITORING_RG = "polyclaw-monitoring-rg"


class MonitoringRoutes:
    """REST handler for monitoring / OpenTelemetry configuration."""

    def __init__(
        self,
        store: MonitoringConfigStore,
        az: AzureCLI | None = None,
        deploy_store: DeployStateStore | None = None,
    ) -> None:
        self._store = store
        self._az = az
        self._deploy_store = deploy_store

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/monitoring/config", self._get_config)
        router.add_post("/api/monitoring/config", self._save_config)
        router.add_get("/api/monitoring/status", self._get_status)
        router.add_post("/api/monitoring/test", self._test_connection)
        router.add_post("/api/monitoring/provision", self._provision)
        router.add_delete("/api/monitoring/provision", self._decommission)

    async def _get_config(self, _req: web.Request) -> web.Response:
        data = self._store.to_dict()
        status = get_status()
        # In split-container mode the admin process does not run the OTel
        # exporter -- that happens in the runtime container.  If the config
        # says monitoring is enabled and a connection string is set, report
        # active=True so the frontend shows the correct status even when
        # this process is not the one exporting telemetry.
        if not status["active"] and self._store.is_configured:
            status["active"] = True
        data["otel_status"] = status
        return web.json_response(data)

    async def _save_config(self, req: web.Request) -> web.Response:
        data = await req.json()

        connection_string = data.get("connection_string")
        enabled = data.get("enabled")
        sampling_ratio = data.get("sampling_ratio")
        enable_live_metrics = data.get("enable_live_metrics")

        updates: dict = {}
        if connection_string is not None:
            updates["connection_string"] = connection_string
        if enabled is not None:
            updates["enabled"] = bool(enabled)
        if sampling_ratio is not None:
            updates["sampling_ratio"] = max(0.0, min(1.0, float(sampling_ratio)))
        if enable_live_metrics is not None:
            updates["enable_live_metrics"] = bool(enable_live_metrics)

        if updates:
            self._store.update(**updates)

        # Apply OTel config changes
        cfg = self._store.config
        if cfg.enabled and cfg.connection_string:
            ok = configure_otel(
                cfg.connection_string,
                sampling_ratio=cfg.sampling_ratio,
                enable_live_metrics=cfg.enable_live_metrics,
            )
            if ok:
                return web.json_response({
                    "status": "ok",
                    "message": "Monitoring enabled -- telemetry is being exported to Application Insights.",
                })
            return web.json_response({
                "status": "warning",
                "message": (
                    "Config saved but OTel could not be initialised. "
                    "Ensure azure-monitor-opentelemetry is installed."
                ),
            })

        if not cfg.enabled and is_active():
            shutdown_otel()
            return web.json_response({
                "status": "ok",
                "message": "Monitoring disabled. OTel providers shut down. Full cleanup requires a restart.",
            })

        return web.json_response({"status": "ok", "message": "Monitoring configuration saved."})

    async def _get_status(self, _req: web.Request) -> web.Response:
        return web.json_response(get_status())

    async def _test_connection(self, req: web.Request) -> web.Response:
        """Quick validation that the connection string looks correct."""
        data = await req.json()
        cs = data.get("connection_string", "")
        if not cs:
            return web.json_response(
                {"status": "error", "message": "No connection string provided."},
                status=400,
            )

        # Parse the connection string to validate format
        parts: dict[str, str] = {}
        for segment in cs.split(";"):
            if "=" in segment:
                key, _, value = segment.partition("=")
                parts[key.strip().lower()] = value.strip()

        ikey = parts.get("instrumentationkey", "")
        ingestion = parts.get("ingestionendpoint", "")

        if not ikey:
            return web.json_response(
                {"status": "error", "message": "Connection string missing InstrumentationKey."},
                status=400,
            )
        if not ingestion:
            return web.json_response(
                {"status": "error", "message": "Connection string missing IngestionEndpoint."},
                status=400,
            )

        return web.json_response({
            "status": "ok",
            "message": "Connection string format is valid.",
            "instrumentation_key": f"{ikey[:8]}...{ikey[-4:]}" if len(ikey) > 12 else ikey,
            "ingestion_endpoint": ingestion,
        })

    # ------------------------------------------------------------------
    # Provisioning -- create / destroy App Insights via Azure CLI
    # ------------------------------------------------------------------

    async def _provision(self, req: web.Request) -> web.Response:
        """Provision a Log Analytics workspace + Application Insights resource."""
        if not self._az:
            return _no_az()

        if self._store.is_provisioned:
            return web.json_response({
                "status": "ok",
                "message": f"Already provisioned: {self._store.config.app_insights_name}",
                "steps": [],
                **self._store.to_dict(),
            })

        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:
            body = {}

        location = body.get("location", "eastus").strip()
        rg = body.get("resource_group", "").strip() or _DEFAULT_MONITORING_RG
        suffix = _secrets.token_hex(4)
        ai_name = body.get("app_insights_name", "").strip() or f"polyclaw-insights-{suffix}"
        ws_name = body.get("workspace_name", "").strip() or f"polyclaw-logs-{suffix}"

        steps: list[dict[str, Any]] = []

        # 1. Ensure the application-insights CLI extension is installed
        if not await self._ensure_extension(steps):
            return _fail_response(steps)

        # 2. Ensure resource group
        if not await self._ensure_rg(rg, location, steps):
            return _fail_response(steps)

        # 3. Create Log Analytics workspace
        ws_id = await self._create_workspace(rg, location, ws_name, steps)
        if not ws_id:
            return _fail_response(steps)

        # 4. Create Application Insights component linked to the workspace
        cs = await self._create_app_insights(rg, location, ai_name, ws_id, steps)
        if not cs:
            return _fail_response(steps)

        # 5. Persist metadata and enable OTel
        sub_id = ""
        if self._az:
            account = self._az.account_info()
            sub_id = account.get("id", "") if account else ""
        self._store.set_provisioned_metadata(
            app_insights_name=ai_name,
            workspace_name=ws_name,
            resource_group=rg,
            location=location,
            connection_string=cs,
            subscription_id=sub_id,
        )
        steps.append({"step": "save_config", "status": "ok", "detail": "Configuration saved"})

        # Register resources in deploy state
        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.add_resource(
                    resource_type="log_analytics_workspace",
                    resource_group=rg,
                    resource_name=ws_name,
                    purpose="Monitoring Log Analytics workspace",
                )
                rec.add_resource(
                    resource_type="app_insights",
                    resource_group=rg,
                    resource_name=ai_name,
                    purpose="Application Insights for OTel telemetry",
                )
                if rg not in rec.resource_groups:
                    rec.resource_groups.append(rg)
                self._deploy_store.update(rec)

        # 6. Activate OTel immediately
        configure_otel(
            cs,
            sampling_ratio=self._store.config.sampling_ratio,
            enable_live_metrics=self._store.config.enable_live_metrics,
        )
        steps.append({"step": "otel_bootstrap", "status": "ok", "detail": "OTel configured"})

        logger.info(
            "[monitoring.provision] App Insights '%s' provisioned (rg=%s)",
            ai_name, rg,
        )
        return web.json_response({
            "status": "ok",
            "message": f"Application Insights '{ai_name}' provisioned and monitoring enabled.",
            "steps": steps,
            **self._store.to_dict(),
        })

    async def _decommission(self, _req: web.Request) -> web.Response:
        """Delete the provisioned App Insights + Log Analytics resources."""
        if not self._az:
            return _no_az()
        if not self._store.is_provisioned:
            return web.json_response(
                {"status": "error", "message": "No monitoring resources provisioned."},
                status=400,
            )

        steps: list[dict[str, Any]] = []
        ai_name = self._store.config.app_insights_name
        ws_name = self._store.config.workspace_name
        rg = self._store.config.resource_group

        # Shut down OTel first
        if is_active():
            shutdown_otel()
            steps.append({"step": "otel_shutdown", "status": "ok", "detail": "OTel shut down"})

        # Delete App Insights
        ok, msg = await run_sync(
            self._az.ok,
            "monitor", "app-insights", "component", "delete",
            "--app", ai_name, "--resource-group", rg, "--yes",
        )
        steps.append({
            "step": "delete_app_insights",
            "status": "ok" if ok else "failed",
            "detail": f"Deleted {ai_name}" if ok else (msg or "Unknown error"),
        })

        # Delete Log Analytics workspace
        wok, wmsg = await run_sync(
            self._az.ok,
            "monitor", "log-analytics", "workspace", "delete",
            "--workspace-name", ws_name, "--resource-group", rg, "--yes", "--force",
        )
        steps.append({
            "step": "delete_workspace",
            "status": "ok" if wok else "failed",
            "detail": f"Deleted {ws_name}" if wok else (wmsg or "Unknown error"),
        })

        # Optionally delete the resource group if it was the default
        if rg == _DEFAULT_MONITORING_RG:
            rg_ok, rg_msg = await run_sync(
                self._az.ok,
                "group", "delete", "--name", rg, "--yes", "--no-wait",
            )
            steps.append({
                "step": "delete_rg",
                "status": "ok" if rg_ok else "failed",
                "detail": f"Deleting {rg}" if rg_ok else (rg_msg or "Unknown error"),
            })

        # Clean deploy state
        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                rec.resources = [
                    r for r in rec.resources
                    if r.resource_name not in (ai_name, ws_name)
                ]
                if rg in rec.resource_groups:
                    rec.resource_groups.remove(rg)
                self._deploy_store.update(rec)

        self._store.clear_provisioned_metadata()
        steps.append({"step": "clear_config", "status": "ok", "detail": "Configuration cleared"})

        logger.info("[monitoring.decommission] App Insights '%s' removed", ai_name)
        return web.json_response({
            "status": "ok",
            "message": f"Application Insights '{ai_name}' decommissioned.",
            "steps": steps,
            **self._store.to_dict(),
        })

    # -- internal helpers --

    async def _ensure_extension(self, steps: list[dict[str, Any]]) -> bool:
        """Ensure the ``application-insights`` CLI extension is installed."""
        ok, msg = await run_sync(
            self._az.ok,
            "extension", "add", "--name", "application-insights", "--yes",
        )
        steps.append({
            "step": "cli_extension",
            "status": "ok" if ok else "failed",
            "detail": "application-insights extension ready" if ok else (msg or "Unknown error"),
        })
        return ok

    async def _ensure_rg(
        self, rg: str, location: str, steps: list[dict[str, Any]]
    ) -> bool:
        existing = await run_sync(self._az.json, "group", "show", "--name", rg)
        if existing:
            steps.append({"step": "resource_group", "status": "ok", "detail": f"{rg} (existing)"})
            return True

        tag_args: list[str] = []
        if self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec:
                tag_args = ["--tags", f"polyclaw_deploy={rec.tag}"]

        result = await run_sync(
            self._az.json,
            "group", "create", "--name", rg, "--location", location, *tag_args,
        )
        ok = bool(result)
        steps.append({
            "step": "resource_group",
            "status": "ok" if ok else "failed",
            "detail": rg if ok else (self._az.last_stderr or "Unknown error"),
        })
        if ok and self._deploy_store:
            rec = self._deploy_store.current_local()
            if rec and rg not in rec.resource_groups:
                rec.resource_groups.append(rg)
                self._deploy_store.update(rec)
        return ok

    async def _create_workspace(
        self,
        rg: str,
        location: str,
        ws_name: str,
        steps: list[dict[str, Any]],
    ) -> str | None:
        """Create a Log Analytics workspace. Returns the workspace resource ID."""
        logger.info("[monitoring.provision] Creating Log Analytics workspace '%s'...", ws_name)
        result = await run_sync(
            self._az.json,
            "monitor", "log-analytics", "workspace", "create",
            "--workspace-name", ws_name, "--resource-group", rg, "--location", location,
        )
        if not result or not isinstance(result, dict):
            err = self._az.last_stderr or "Unknown error"
            steps.append({"step": "create_workspace", "status": "failed", "detail": err[:300]})
            return None

        ws_id = result.get("id", "")
        steps.append({
            "step": "create_workspace", "status": "ok",
            "detail": f"{ws_name} created",
        })
        return ws_id

    async def _create_app_insights(
        self,
        rg: str,
        location: str,
        ai_name: str,
        ws_id: str,
        steps: list[dict[str, Any]],
    ) -> str | None:
        """Create an Application Insights component. Returns the connection string."""
        logger.info("[monitoring.provision] Creating Application Insights '%s'...", ai_name)
        result = await run_sync(
            self._az.json,
            "monitor", "app-insights", "component", "create",
            "--app", ai_name,
            "--location", location,
            "--resource-group", rg,
            "--workspace", ws_id,
            "--application-type", "web",
        )
        if not result or not isinstance(result, dict):
            err = self._az.last_stderr or "Unknown error"
            steps.append({"step": "create_app_insights", "status": "failed", "detail": err[:300]})
            return None

        cs = result.get("connectionString", "")
        if not cs:
            steps.append({
                "step": "create_app_insights", "status": "failed",
                "detail": "Resource created but connectionString not found in response",
            })
            return None

        steps.append({
            "step": "create_app_insights", "status": "ok",
            "detail": f"{ai_name} created",
        })
        return cs

