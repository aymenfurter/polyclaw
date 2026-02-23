"""Plugin configuration -- persistent JSON store.

Stores plugin state (enabled/disabled, setup completed) in a JSON file.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config.settings import cfg

logger = logging.getLogger(__name__)

_DEFAULT_STATE: dict[str, Any] = {
    "enabled": False,
    "setup_completed": False,
    "installed_at": None,
}


class PluginConfigStore:
    """JSON-file-backed plugin state store."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (cfg.data_dir / "plugins.json")
        self._plugins: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def get_state(self, plugin_id: str) -> dict[str, Any]:
        return self._plugins.get(plugin_id, dict(_DEFAULT_STATE))

    def list_states(self) -> dict[str, dict[str, Any]]:
        return dict(self._plugins)

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        if plugin_id not in self._plugins:
            self._plugins[plugin_id] = dict(_DEFAULT_STATE)
        self._plugins[plugin_id]["enabled"] = enabled
        if enabled and not self._plugins[plugin_id].get("installed_at"):
            self._plugins[plugin_id]["installed_at"] = datetime.now(UTC).isoformat()
        self._save()

    def mark_setup_completed(self, plugin_id: str) -> None:
        if plugin_id not in self._plugins:
            self._plugins[plugin_id] = dict(_DEFAULT_STATE)
        self._plugins[plugin_id]["setup_completed"] = True
        self._save()

    def reset(self, plugin_id: str) -> None:
        self._plugins[plugin_id] = dict(_DEFAULT_STATE)
        self._save()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            self._plugins = raw.get("plugins", {})
        except Exception as exc:
            logger.warning(
                "Failed to load plugin config from %s: %s", self._path, exc, exc_info=True,
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"plugins": self._plugins}
        self._path.write_text(json.dumps(data, indent=2) + "\n")
