"""Monitoring API routes -- /api/monitoring."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from ...services.cloud.azure import AzureCLI
from ...services.deployment.bicep_deployer import BicepDeployer, BicepDeployRequest
from ...services.otel import configure_otel, get_status, is_active, shutdown_otel
from ...state.deploy_state import DeployStateStore
from ...state.monitoring_config import MonitoringConfigStore
from ...util.async_helpers import run_sync
from ._helpers import api_handler, error_response, ok_response, parse_json
from ._helpers import fail_response as _fail_response
from ._helpers import no_az as _no_az

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
        self._bicep = BicepDeployer(az, deploy_store) if az and deploy_store else None

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

    @api_handler
    async def _save_config(self, req: web.Request) -> web.Response:
        data = await parse_json(req)

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
                return ok_response(
                    message="Monitoring enabled -- telemetry is being exported to Application Insights.",
                )
            return web.json_response({
                "status": "warning",
                "message": (
                    "Config saved but OTel could not be initialised. "
                    "Ensure azure-monitor-opentelemetry is installed."
                ),
            })

        if not cfg.enabled and is_active():
            shutdown_otel()
            return ok_response(
                message="Monitoring disabled. OTel providers shut down. Full cleanup requires a restart.",
            )

        return ok_response(message="Monitoring configuration saved.")

    async def _get_status(self, _req: web.Request) -> web.Response:
        return web.json_response(get_status())

    @api_handler
    async def _test_connection(self, req: web.Request) -> web.Response:
        """Quick validation that the connection string looks correct."""
        data = await parse_json(req)
        cs = data.get("connection_string", "")
        if not cs:
            return error_response("No connection string provided.")

        # Parse the connection string to validate format
        parts: dict[str, str] = {}
        for segment in cs.split(";"):
            if "=" in segment:
                key, _, value = segment.partition("=")
                parts[key.strip().lower()] = value.strip()

        ikey = parts.get("instrumentationkey", "")
        ingestion = parts.get("ingestionendpoint", "")

        if not ikey:
            return error_response("Connection string missing InstrumentationKey.")
        if not ingestion:
            return error_response("Connection string missing IngestionEndpoint.")

        return ok_response(
            message="Connection string format is valid.",
            instrumentation_key=f"{ikey[:8]}...{ikey[-4:]}" if len(ikey) > 12 else ikey,
            ingestion_endpoint=ingestion,
        )

    # ------------------------------------------------------------------
    # Provisioning -- create / destroy App Insights via Azure CLI
    # ------------------------------------------------------------------

    async def _provision(self, req: web.Request) -> web.Response:
        """Provision Log Analytics + Application Insights via the central Bicep template."""
        if self._store.is_provisioned:
            return ok_response(
                message=f"Already provisioned: {self._store.config.app_insights_name}",
                steps=[],
                **self._store.to_dict(),
            )

        if not self._bicep:
            return _no_az()

        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:
            body = {}

        location = body.get("location", "eastus").strip()
        rg = body.get("resource_group", "").strip() or _DEFAULT_MONITORING_RG

        bicep_req = BicepDeployRequest(
            resource_group=rg,
            location=location,
            deploy_foundry=False,
            deploy_key_vault=False,
            deploy_monitoring=True,
        )
        result = await run_sync(self._bicep.deploy, bicep_req)

        if not result.ok or not result.app_insights_connection_string:
            return _fail_response(result.steps)

        # Persist metadata and enable OTel
        sub_id = ""
        if self._az:
            account = self._az.account_info()
            sub_id = account.get("id", "") if account else ""
        self._store.set_provisioned_metadata(
            app_insights_name=result.app_insights_name,
            workspace_name=result.log_analytics_workspace_name,
            resource_group=rg,
            location=location,
            connection_string=result.app_insights_connection_string,
            subscription_id=sub_id,
        )
        result.steps.append({"step": "save_config", "status": "ok", "detail": "Configuration saved"})

        # Activate OTel immediately
        configure_otel(
            result.app_insights_connection_string,
            sampling_ratio=self._store.config.sampling_ratio,
            enable_live_metrics=self._store.config.enable_live_metrics,
        )
        result.steps.append({"step": "otel_bootstrap", "status": "ok", "detail": "OTel configured"})

        logger.info(
            "[monitoring.provision] App Insights '%s' provisioned via Bicep (rg=%s)",
            result.app_insights_name, rg,
        )
        return ok_response(
            message=f"Application Insights '{result.app_insights_name}' provisioned and monitoring enabled.",
            steps=result.steps,
            **self._store.to_dict(),
        )

    async def _decommission(self, _req: web.Request) -> web.Response:
        """Delete the provisioned App Insights + Log Analytics resources."""
        if not self._az:
            return _no_az()
        if not self._store.is_provisioned:
            return error_response("No monitoring resources provisioned.")

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
        return ok_response(
            message=f"Application Insights '{ai_name}' decommissioned.",
            steps=steps,
            **self._store.to_dict(),
        )
