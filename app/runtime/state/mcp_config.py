"""MCP server configuration -- persistent JSON store.

Stores MCP server definitions (local + remote) in a JSON file. The agent
reads this on every session creation so changes take effect immediately.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..config.settings import cfg

logger = logging.getLogger(__name__)

_BUILTIN_SERVERS: dict[str, dict[str, Any]] = {
    "playwright": {
        "type": "local",
        "command": "npx",
        "args": [
            "-y", "@playwright/mcp@latest", "--browser", "chromium",
            "--headless", "--isolated", "--viewport-size", "1366x768",
        ],
        "env": {"PLAYWRIGHT_CHROMIUM_ARGS": "--no-sandbox --disable-setuid-sandbox"},
        "tools": ["*"],
        "enabled": True,
        "builtin": True,
        "description": "Browser automation via Playwright MCP",
    },
    "microsoft-learn": {
        "type": "http",
        "url": "https://learn.microsoft.com/api/mcp",
        "tools": ["*"],
        "enabled": False,
        "builtin": True,
        "description": "Search and fetch official Microsoft Learn documentation",
    },
    "azure-mcp-server": {
        "type": "local",
        "command": "npx",
        "args": ["-y", "@azure/mcp@latest", "server", "start"],
        "env": {"DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1"},
        "tools": ["*"],
        "enabled": False,
        "builtin": True,
        "description": "Manage Azure resources via the Azure MCP Server (requires az login)",
    },
    "github-mcp-server": {
        "type": "local",
        "command": "sh",
        "args": [
            "-c",
            "GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_PERSONAL_ACCESS_TOKEN:-$(gh auth token)} "
            "npx -y @github/mcp-server",
        ],
        "tools": ["*"],
        "enabled": False,
        "builtin": True,
        "description": "GitHub API integration (uses gh CLI auth or GITHUB_PERSONAL_ACCESS_TOKEN)",
    },
}

_VALID_TYPES = ("local", "stdio", "http", "sse")
_STRIP_KEYS = {"enabled", "builtin", "description", "name"}


class McpConfigStore:
    """JSON-file-backed MCP server configuration."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (cfg.data_dir / "mcp_servers.json")
        self._servers: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def list_servers(self) -> list[dict[str, Any]]:
        return [{"name": name, **server} for name, server in self._servers.items()]

    def get_server(self, name: str) -> dict[str, Any] | None:
        server = self._servers.get(name)
        return {"name": name, **server} if server else None

    def get_enabled_servers(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for name, server in self._servers.items():
            if not server.get("enabled", True):
                continue
            entry = {k: v for k, v in server.items() if k not in _STRIP_KEYS}
            if entry.get("type") == "remote":
                entry["type"] = "http"
            result[name] = entry
        return result

    def add_server(
        self,
        name: str,
        server_type: str,
        *,
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str = "",
        tools: list[str] | None = None,
        enabled: bool = True,
        description: str = "",
    ) -> dict[str, Any]:
        if not name:
            raise ValueError("Server name is required")
        if server_type not in _VALID_TYPES:
            raise ValueError(f"type must be one of {_VALID_TYPES}")

        is_remote = server_type in ("http", "sse")
        if not is_remote and not command:
            raise ValueError("command is required for local servers")
        if is_remote and not url:
            raise ValueError("url is required for remote (http/sse) servers")

        entry: dict[str, Any] = {
            "type": server_type,
            "enabled": enabled,
            "builtin": False,
            "description": description,
        }
        if is_remote:
            entry["url"] = url
        else:
            entry["command"] = command
            if args:
                entry["args"] = args
            if env:
                entry["env"] = env
        entry["tools"] = tools or ["*"]

        self._servers[name] = entry
        self._save()
        return {"name": name, **entry}

    def update_server(self, name: str, **kwargs: Any) -> dict[str, Any] | None:
        if name not in self._servers:
            return None
        server = self._servers[name]
        for key, value in kwargs.items():
            if key in ("name", "builtin"):
                continue
            server[key] = value
        self._save()
        return {"name": name, **server}

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self._servers:
            return False
        self._servers[name]["enabled"] = enabled
        self._save()
        return True

    def remove_server(self, name: str) -> bool:
        if name not in self._servers:
            return False
        if self._servers[name].get("builtin", False):
            raise ValueError(f"Cannot remove built-in server '{name}'. Disable it instead.")
        del self._servers[name]
        self._save()
        return True

    def _load(self) -> None:
        for name, server in _BUILTIN_SERVERS.items():
            if name not in self._servers:
                self._servers[name] = dict(server)

        if not self._path.exists():
            self._save()
            return

        try:
            raw = json.loads(self._path.read_text())
            stored = raw.get("servers", {})
            for name, server in stored.items():
                self._servers[name] = server

            dirty = False
            for name, builtin in _BUILTIN_SERVERS.items():
                if name not in self._servers:
                    self._servers[name] = dict(builtin)
                    dirty = True
                elif self._servers[name].get("builtin", False):
                    user_enabled = self._servers[name].get("enabled", builtin.get("enabled", True))
                    self._servers[name] = dict(builtin)
                    self._servers[name]["enabled"] = user_enabled
                    dirty = True
            if dirty:
                self._save()
        except Exception as exc:
            logger.warning(
                "Failed to load MCP config from %s: %s", self._path, exc, exc_info=True,
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"servers": self._servers}
        self._path.write_text(json.dumps(data, indent=2) + "\n")
