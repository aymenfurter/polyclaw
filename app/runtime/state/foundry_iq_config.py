"""Foundry IQ configuration -- Azure AI Search + embedding settings."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from ._base import BaseConfigStore

logger = logging.getLogger(__name__)


@dataclass
class FoundryIQConfig:
    enabled: bool = False
    search_endpoint: str = ""
    search_api_key: str = ""
    index_name: str = "polyclaw-memories"
    embedding_endpoint: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 3072
    index_schedule: str = "daily"
    last_indexed_at: str = ""
    resource_group: str = ""
    location: str = ""
    search_resource_name: str = ""
    openai_resource_name: str = ""
    openai_deployment_name: str = ""
    provisioned: bool = False


class FoundryIQConfigStore(BaseConfigStore[FoundryIQConfig]):
    """JSON-file-backed Foundry IQ configuration."""

    _config_type = FoundryIQConfig
    _default_filename = "foundry_iq.json"
    _log_label = "Foundry IQ config"
    _SECRET_FIELDS = frozenset({"search_api_key", "embedding_api_key"})
    _secret_prefix = "foundryiq-"

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def is_configured(self) -> bool:
        c = self._config
        if c.provisioned:
            return bool(c.search_endpoint and c.embedding_endpoint)
        return bool(
            c.search_endpoint and c.search_api_key
            and c.embedding_endpoint and c.embedding_api_key
        )

    @property
    def is_provisioned(self) -> bool:
        return self._config.provisioned

    def to_dict(self) -> dict[str, Any]:
        return asdict(self._config)

    def to_safe_dict(self) -> dict[str, Any]:
        data = asdict(self._config)
        for key in ("search_api_key", "embedding_api_key"):
            if data.get(key):
                data[key] = "****"
        return data

    def save(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self._config, k):
                if k == "enabled" and isinstance(v, str):
                    v = v.lower() in ("true", "1", "yes")
                elif k == "embedding_dimensions" and isinstance(v, str):
                    v = int(v)
                setattr(self._config, k, v)
        self._save()

    def set_last_indexed(self, timestamp: str) -> None:
        self._config.last_indexed_at = timestamp
        self._save()

    def clear_provisioning(self) -> None:
        for attr in (
            "resource_group", "location", "search_resource_name",
            "openai_resource_name", "openai_deployment_name",
            "search_endpoint", "search_api_key",
            "embedding_endpoint", "embedding_api_key",
        ):
            setattr(self._config, attr, "")
        self._config.provisioned = False
        self._config.enabled = False
        self._save()

    def _apply_raw(self, raw: dict[str, Any]) -> None:
        for k in FoundryIQConfig.__dataclass_fields__:
            if k in raw:
                value = raw[k]
                if k in self._SECRET_FIELDS and isinstance(value, str):
                    value = self._resolve_secret(value)
                setattr(self._config, k, value)

    def _save_data(self) -> dict[str, Any]:
        data = asdict(self._config)
        return self._store_secrets(data)


# -- singleton -------------------------------------------------------------

_store: FoundryIQConfigStore | None = None


def get_foundry_iq_config() -> FoundryIQConfigStore:
    global _store
    if _store is None:
        _store = FoundryIQConfigStore()
    return _store


def _reset_store() -> None:
    global _store
    _store = None


from ..util.singletons import register_singleton
register_singleton(_reset_store)
