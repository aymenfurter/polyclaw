"""Sandbox configuration -- whitelist/blacklist and session pool metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._base import BaseConfigStore

DEFAULT_WHITELIST: list[str] = [
    "media", "memory", "notes", "sessions", "skills",
    ".copilot", ".env", ".workiq.json", "agent_profile.json",
    "conversation_refs.json", "infra.json", "interaction_log.json",
    "mcp_servers.json", "plugins.json", "scheduler.json",
    "skill_usage.json", "SOUL.md",
]

BLACKLIST: frozenset[str] = frozenset({
    ".azure", ".cache", ".config", ".IdentityService",
    ".net", ".npm", ".pki",
})


@dataclass
class SandboxConfig:
    enabled: bool = False
    sync_data: bool = True
    session_pool_endpoint: str = ""
    whitelist: list[str] = field(default_factory=lambda: list(DEFAULT_WHITELIST))
    resource_group: str = ""
    location: str = ""
    pool_name: str = ""
    pool_id: str = ""


class SandboxConfigStore(BaseConfigStore[SandboxConfig]):
    """JSON-file-backed sandbox configuration."""

    _config_type = SandboxConfig
    _default_filename = "sandbox.json"
    _log_label = "sandbox config"

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def sync_data(self) -> bool:
        return self._config.sync_data

    @property
    def session_pool_endpoint(self) -> str:
        return self._config.session_pool_endpoint

    @property
    def whitelist(self) -> list[str]:
        return list(self._config.whitelist)

    @property
    def resource_group(self) -> str:
        return self._config.resource_group

    @property
    def location(self) -> str:
        return self._config.location

    @property
    def pool_name(self) -> str:
        return self._config.pool_name

    @property
    def pool_id(self) -> str:
        return self._config.pool_id

    @property
    def is_provisioned(self) -> bool:
        return bool(self._config.pool_name and self._config.session_pool_endpoint)

    def set_enabled(self, enabled: bool) -> None:
        self._config.enabled = enabled
        self._save()

    def set_sync_data(self, sync_data: bool) -> None:
        self._config.sync_data = sync_data
        self._save()

    def set_session_pool_endpoint(self, endpoint: str) -> None:
        self._config.session_pool_endpoint = endpoint.rstrip("/")
        self._save()

    def set_whitelist(self, whitelist: list[str]) -> None:
        self._config.whitelist = [w for w in whitelist if w not in BLACKLIST]
        self._save()

    def add_whitelist_item(self, item: str) -> bool:
        if item in BLACKLIST:
            return False
        if item not in self._config.whitelist:
            self._config.whitelist.append(item)
            self._save()
        return True

    def remove_whitelist_item(self, item: str) -> None:
        if item in self._config.whitelist:
            self._config.whitelist.remove(item)
            self._save()

    def reset_whitelist(self) -> None:
        self._config.whitelist = list(DEFAULT_WHITELIST)
        self._save()

    def set_pool_metadata(
        self,
        *,
        resource_group: str,
        location: str,
        pool_name: str,
        pool_id: str,
        endpoint: str,
    ) -> None:
        self._config.resource_group = resource_group
        self._config.location = location
        self._config.pool_name = pool_name
        self._config.pool_id = pool_id
        self._config.session_pool_endpoint = endpoint.rstrip("/")
        self._save()

    def clear_pool_metadata(self) -> None:
        self._config.resource_group = ""
        self._config.location = ""
        self._config.pool_name = ""
        self._config.pool_id = ""
        self._config.session_pool_endpoint = ""
        self._save()

    def update(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if k == "whitelist":
                self.set_whitelist(v)
            elif hasattr(self._config, k):
                setattr(self._config, k, v)
        self._save()


