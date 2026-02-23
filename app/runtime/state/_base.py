"""Base class for dataclass-backed JSON config stores."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Generic, TypeVar

from ..config.settings import cfg

logger = logging.getLogger(__name__)

C = TypeVar("C")


class BaseConfigStore(Generic[C]):
    """JSON-file-backed config store using a dataclass for schema.

    Subclasses must set class variables:

    - ``_config_type``: the dataclass class used for config schema
    - ``_default_filename``: default JSON filename inside ``cfg.data_dir``

    Optional class variables:

    - ``_log_label``: human label used in warning messages (defaults to filename)

    Override ``_apply_raw`` to customise how JSON fields are mapped onto the
    config dataclass (e.g. secret resolution).  Override ``_save_data`` to
    customise the dict that is serialised to disk (e.g. secret storage).
    """

    _config_type: type[C]
    _default_filename: str
    _log_label: str = ""
    _SECRET_FIELDS: frozenset[str] = frozenset()
    _secret_prefix: str = ""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (cfg.data_dir / self._default_filename)
        self._config: C = self._config_type()
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def config(self) -> C:
        return self._config

    def to_dict(self) -> dict[str, Any]:
        """Return the config as a plain dict."""
        return asdict(self._config)

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            self._apply_raw(raw)
        except Exception as exc:
            label = self._log_label or self._default_filename
            logger.warning(
                "Failed to load %s from %s: %s", label, self._path, exc, exc_info=True,
            )

    def _apply_raw(self, raw: dict[str, Any]) -> None:
        """Populate config fields from a raw JSON dict.

        Default implementation sets every dataclass field found in *raw*.
        Override for custom deserialisation (e.g. secret resolution).
        """
        for field_name in self._config_type.__dataclass_fields__:
            if field_name in raw:
                setattr(self._config, field_name, raw[field_name])

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._save_data(), indent=2) + "\n")

    def _save_data(self) -> dict[str, Any]:
        """Return the data dict to serialise.

        Default implementation returns ``dataclasses.asdict(self._config)``.
        Override for custom serialisation (e.g. secret storage).
        """
        return asdict(self._config)

    # -- secret helpers ----------------------------------------------------

    def _store_secrets(self, data: dict[str, Any]) -> dict[str, Any]:
        """Replace secret fields with Key Vault references before persisting.

        Only operates when ``_SECRET_FIELDS`` is non-empty and Key Vault is
        enabled.  Uses ``_secret_prefix`` to namespace the secret names.
        """
        from ..services.keyvault import env_key_to_secret_name, is_kv_ref, kv

        result = dict(data)
        if not kv.enabled or not self._SECRET_FIELDS:
            return result
        prefix = self._secret_prefix
        for k in self._SECRET_FIELDS:
            val = result.get(k, "")
            if val and not is_kv_ref(val):
                try:
                    ref = kv.store(env_key_to_secret_name(f"{prefix}{k}"), val)
                    result[k] = ref
                except Exception as exc:
                    logger.warning(
                        "Failed to store secret %s in KV: %s", k, exc, exc_info=True,
                    )
        return result

    @staticmethod
    def _resolve_secret(value: Any) -> Any:
        """Resolve a possible Key Vault reference back to its plaintext."""
        if not isinstance(value, str):
            return value
        from ..services.keyvault import resolve_if_kv_ref

        return resolve_if_kv_ref(value)
