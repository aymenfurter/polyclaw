"""Monitoring configuration -- OpenTelemetry and Application Insights settings."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config.settings import cfg

logger = logging.getLogger(__name__)


def _url_encode_slashes(path: str) -> str:
    """Percent-encode forward slashes for Azure portal resource-ID URL fragments."""
    return path.replace("/", "%2F")


@dataclass
class MonitoringConfig:
    enabled: bool = False
    connection_string: str = ""
    sampling_ratio: float = 1.0
    enable_live_metrics: bool = False
    instrumentation_options: dict[str, Any] = field(default_factory=dict)
    # Provisioning metadata (filled when deployed via admin GUI)
    provisioned: bool = False
    app_insights_name: str = ""
    workspace_name: str = ""
    resource_group: str = ""
    location: str = ""
    subscription_id: str = ""


class MonitoringConfigStore:
    """JSON-file-backed monitoring / OTel configuration."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (cfg.data_dir / "monitoring.json")
        self._config = MonitoringConfig()
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def config(self) -> MonitoringConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def connection_string(self) -> str:
        return self._config.connection_string

    @property
    def is_configured(self) -> bool:
        return bool(self._config.enabled and self._config.connection_string)

    @property
    def is_provisioned(self) -> bool:
        return self._config.provisioned

    def update(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self._config, k):
                setattr(self._config, k, v)
        self._save()

    def set_provisioned_metadata(
        self,
        *,
        app_insights_name: str,
        workspace_name: str,
        resource_group: str,
        location: str,
        connection_string: str,
        subscription_id: str = "",
    ) -> None:
        """Persist provisioning metadata after a successful deploy."""
        self._config.provisioned = True
        self._config.app_insights_name = app_insights_name
        self._config.workspace_name = workspace_name
        self._config.resource_group = resource_group
        self._config.location = location
        self._config.connection_string = connection_string
        self._config.subscription_id = subscription_id
        self._config.enabled = True
        self._save()

    def clear_provisioned_metadata(self) -> None:
        """Clear all provisioning metadata after decommission."""
        self._config.provisioned = False
        self._config.app_insights_name = ""
        self._config.workspace_name = ""
        self._config.resource_group = ""
        self._config.location = ""
        self._config.connection_string = ""
        self._config.subscription_id = ""
        self._config.enabled = False
        self._save()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self._config)
        # Mask the connection string for API responses
        if d.get("connection_string"):
            cs = d["connection_string"]
            # Show only a short masked summary of the instrumentation key
            parts = cs.split(";")
            ikey = ""
            for part in parts:
                if part.lower().startswith("instrumentationkey="):
                    ikey = part.split("=", 1)[1] if "=" in part else ""
            if ikey and len(ikey) > 12:
                d["connection_string_masked"] = f"{ikey[:8]}{'*' * 20}{ikey[-4:]}"
            else:
                d["connection_string_masked"] = "*" * 20
            d["connection_string_set"] = True
        else:
            d["connection_string_masked"] = ""
            d["connection_string_set"] = False
        # Build portal URL for provisioned resources
        sub = d.get("subscription_id", "")
        rg = d.get("resource_group", "")
        ai_name = d.get("app_insights_name", "")
        if sub and rg and ai_name:
            d["portal_url"] = (
                f"https://portal.azure.com/#@/resource/subscriptions/{sub}"
                f"/resourceGroups/{rg}/providers/Microsoft.Insights"
                f"/components/{ai_name}/overview"
            )
            # Build Grafana Agent Framework dashboard URL (Azure Managed Grafana)
            resource_id = (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Insights/components/{ai_name}"
            )
            d["grafana_dashboard_url"] = (
                "https://portal.azure.com/#view/Microsoft_Azure_Monitoring"
                "/AzureGrafana.ReactView"
                "/GalleryType/microsoft.insights%2Fcomponents"
                f"/ResourceId/{_url_encode_slashes(resource_id)}"
                "/ConfigurationId/AgentFramework"
            )
        else:
            d["portal_url"] = ""
            d["grafana_dashboard_url"] = ""
        # Never send the raw connection string
        del d["connection_string"]
        return d

    def to_dict_full(self) -> dict[str, Any]:
        """Return the full config including secrets -- internal use only."""
        return asdict(self._config)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            self._config = MonitoringConfig(
                enabled=raw.get("enabled", False),
                connection_string=raw.get("connection_string", ""),
                sampling_ratio=raw.get("sampling_ratio", 1.0),
                enable_live_metrics=raw.get("enable_live_metrics", False),
                instrumentation_options=raw.get("instrumentation_options", {}),
                provisioned=raw.get("provisioned", False),
                app_insights_name=raw.get("app_insights_name", ""),
                workspace_name=raw.get("workspace_name", ""),
                resource_group=raw.get("resource_group", ""),
                location=raw.get("location", ""),
                subscription_id=raw.get("subscription_id", ""),
            )
        except Exception as exc:
            logger.warning("Failed to load monitoring config from %s: %s", self._path, exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(asdict(self._config), indent=2) + "\n")
