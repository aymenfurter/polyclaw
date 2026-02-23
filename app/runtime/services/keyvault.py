"""Azure Key Vault integration -- secret storage and resolution."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

KV_REF_PREFIX = "@kv:"
_KV_REF_RE = re.compile(r"^@kv:([a-zA-Z0-9-]{1,127})$")


def is_kv_ref(value: str) -> bool:
    return bool(_KV_REF_RE.match(value))


def make_ref(secret_name: str) -> str:
    return f"{KV_REF_PREFIX}{secret_name}"


def env_key_to_secret_name(key: str) -> str:
    return key.lower().replace("_", "-")


def secret_name_to_env_key(name: str) -> str:
    return name.upper().replace("-", "_")


class KeyVaultClient:
    def __init__(self) -> None:
        self._client: Any = None
        self._url: str | None = None
        self._initialised = False
        self._ip_allowed = False

    @property
    def enabled(self) -> bool:
        self._ensure_init()
        return self._url is not None

    @property
    def url(self) -> str | None:
        self._ensure_init()
        return self._url

    def store(self, name: str, value: str) -> str:
        if not self.enabled:
            return value
        max_retries = 4
        wait = 5.0
        for attempt in range(max_retries):
            try:
                self._client.set_secret(name, value)
                logger.info("Stored secret '%s' in Key Vault", name)
                return make_ref(name)
            except Exception as exc:
                if self._is_firewall_error(exc) and not self._ip_allowed:
                    if self._allow_current_ip():
                        self._ip_allowed = True
                        continue
                if "ForbiddenByRbac" in str(exc) and attempt < max_retries - 1:
                    logger.warning("RBAC not propagated for '%s', retrying in %.0fs...", name, wait)
                    time.sleep(wait)
                    wait = min(wait * 2, 30.0)
                    continue
                raise
        raise RuntimeError(f"Failed to store secret '{name}' after {max_retries} attempts")

    def resolve(self, env: dict[str, str]) -> dict[str, str]:
        if not self.enabled:
            return env
        resolved: dict[str, str] = {}
        for key, value in env.items():
            m = _KV_REF_RE.match(value)
            resolved[key] = self._get_secret(m.group(1)) if m else value
        return resolved

    def resolve_value(self, value: str) -> str:
        if not self.enabled:
            return value
        m = _KV_REF_RE.match(value)
        return self._get_secret(m.group(1)) if m else value

    def delete(self, name: str) -> None:
        if not self.enabled:
            return
        try:
            self._client.begin_delete_secret(name).wait()
        except Exception:
            logger.warning("Failed to delete secret '%s'", name, exc_info=True)

    def list_secrets(self) -> list[str]:
        if not self.enabled:
            return []
        try:
            return [s.name for s in self._client.list_properties_of_secrets()]
        except Exception:
            logger.warning("Failed to list secrets", exc_info=True)
            return []

    def store_env_secret(self, env_key: str, value: str) -> str:
        return self.store(env_key_to_secret_name(env_key), value)

    def reinit(self) -> None:
        self._initialised = False
        self._client = None
        self._url = None
        self._ip_allowed = False

    def _ensure_init(self) -> None:
        if self._initialised:
            return
        self._initialised = True
        url = os.getenv("KEY_VAULT_URL", "").strip().rstrip("/")
        if not url:
            try:
                from ..config.settings import cfg
                url = (cfg.env.read("KEY_VAULT_URL") or "").strip().rstrip("/")
            except Exception:
                pass
        if not url:
            return
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            credential = DefaultAzureCredential(connection_timeout=10)
            self._client = SecretClient(vault_url=url, credential=credential)
            self._url = url
            logger.info("Key Vault enabled: %s", url)
        except Exception:
            logger.exception("Failed to init Key Vault client for %s", url)

    @staticmethod
    def _is_firewall_error(exc: Exception) -> bool:
        msg = str(exc)
        return any(p in msg for p in (
            "ForbiddenByConnection",
            "Client address is not authorized",
            "caller is not a trusted service",
        ))

    def _get_secret(self, name: str, _fw_retries: int = 0) -> str:
        try:
            return self._client.get_secret(name).value or ""
        except Exception as exc:
            if self._is_firewall_error(exc):
                if not self._ip_allowed:
                    if self._allow_current_ip():
                        self._ip_allowed = True
                        return self._get_secret(name)
                elif _fw_retries < 2:
                    time.sleep(60)
                    return self._get_secret(name, _fw_retries=_fw_retries + 1)
            logger.error("Failed to resolve Key Vault secret '%s'", name)
            raise

    def _allow_current_ip(self) -> bool:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=10) as resp:
                public_ip = resp.read().decode().strip()
        except Exception:
            return False

        vault_name = os.getenv("KEY_VAULT_NAME", "").strip()
        if not vault_name and self._url:
            m = re.match(r"https://([^.]+)\.vault\.azure\.net", self._url)
            if m:
                vault_name = m.group(1)
        if not vault_name:
            return False

        vault_rg = os.getenv("KEY_VAULT_RG", "").strip()
        rg_args = ["--resource-group", vault_rg] if vault_rg else []

        try:
            r = subprocess.run(
                ["az", "keyvault", "update", "--name", vault_name,
                 "--public-network-access", "Enabled", "--default-action", "Deny", *rg_args],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                return False

            r = subprocess.run(
                ["az", "keyvault", "network-rule", "add", "--name", vault_name,
                 "--ip-address", f"{public_ip}/32", *rg_args],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                return False
        except Exception:
            return False

        time.sleep(60)
        return True


kv = KeyVaultClient()


def _reset_kv() -> None:
    """Reset the module-level Key Vault singleton (for test isolation)."""
    kv.reinit()


from ..util.singletons import register_singleton  # noqa: E402

register_singleton(_reset_kv)


def resolve_if_kv_ref(value: str) -> str:
    """Resolve a ``@kv:secret-name`` reference, returning the original value if not a ref.

    If the value looks like a KV reference but Key Vault is unavailable or
    resolution fails, returns ``""`` so the raw reference string never
    leaks into config values (e.g. being used as a Telegram token).
    """
    if is_kv_ref(value):
        if not kv.enabled:
            logger.debug(
                "Key Vault reference %r skipped -- KV not configured. "
                "Returning empty string.",
                value,
            )
            return ""
        try:
            return kv.resolve_value(value)
        except Exception:
            logger.error(
                "Failed to resolve Key Vault reference %r. "
                "Returning empty string to prevent the raw reference from leaking.",
                value,
                exc_info=True,
            )
            return ""
    return value
