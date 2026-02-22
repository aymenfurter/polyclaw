"""Application settings -- reads from environment and ``.env`` file."""

from __future__ import annotations

import enum
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ..util.env_file import EnvFile
from ..util.singletons import register_singleton

SECRET_ENV_KEYS: frozenset[str] = frozenset({
    "ADMIN_SECRET",
    "BOT_APP_PASSWORD",
    "GITHUB_TOKEN",
    "ACS_CONNECTION_STRING",
    "AZURE_OPENAI_API_KEY",
})

_BOOTSTRAP_PLAINTEXT_KEYS: frozenset[str] = frozenset({
    "RUNTIME_SP_APP_ID",
    "RUNTIME_SP_PASSWORD",
    "RUNTIME_SP_TENANT",
})


class ServerMode(enum.Enum):
    combined = "combined"
    admin = "admin"
    runtime = "runtime"


@dataclass
class BotConfig:
    resource_group: str = "polyclaw-rg"
    location: str = "eastus"
    display_name: str = "polyclaw"
    bot_handle: str = ""


@dataclass
class VoiceConfig:
    acs_connection_string: str = ""
    acs_source_number: str = ""
    voice_target_number: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_realtime_deployment: str = ""
    acs_callback_token: str = ""
    acs_resource_id: str = ""


@dataclass
class AdminConfig:
    port: int = 8000
    lockdown_mode: bool = False
    tunnel_restricted: bool = False


@dataclass
class ModelConfig:
    copilot_model: str = "claude-sonnet-4.6"
    copilot_agent: str = ""


class Settings:

    _DATA_DIR_ENV: ClassVar[str] = "POLYCLAW_DATA_DIR"

    def __init__(self) -> None:
        dotenv = os.getenv("DOTENV_PATH")
        if not dotenv:
            data_dir = os.getenv(self._DATA_DIR_ENV)
            if data_dir:
                dotenv = str(Path(data_dir) / ".env")
            else:
                dotenv = ".env"
        self.env = EnvFile(dotenv)
        self._acs_callback_token: str = ""
        self.reload()

    def reload(self) -> None:
        e = self._read

        raw_mode = os.getenv("POLYCLAW_SERVER_MODE", "combined").lower()
        try:
            self.server_mode: ServerMode = ServerMode(raw_mode)
        except ValueError:
            self.server_mode = ServerMode.combined

        self.bot_app_id: str = e("BOT_APP_ID")
        self.bot_app_password: str = e("BOT_APP_PASSWORD")
        self.bot_app_tenant_id: str = e("BOT_APP_TENANT_ID")
        self.bot_port: int = int(e("BOT_PORT") or "3978")

        self.github_token: str = e("GITHUB_TOKEN")

        self.copilot_model: str = e("COPILOT_MODEL") or "claude-sonnet-4.6"
        self.copilot_agent: str = e("COPILOT_AGENT") or ""

        self.admin_port: int = int(e("ADMIN_PORT") or "9090")
        self.lockdown_mode: bool = bool(e("LOCKDOWN_MODE"))
        self.tunnel_restricted: bool = bool(e("TUNNEL_RESTRICTED"))

        self.acs_connection_string: str = e("ACS_CONNECTION_STRING")
        self.acs_source_number: str = e("ACS_SOURCE_NUMBER")
        self.voice_target_number: str = e("VOICE_TARGET_NUMBER")
        self.azure_openai_endpoint: str = e("AZURE_OPENAI_ENDPOINT")
        self.azure_openai_api_key: str = e("AZURE_OPENAI_API_KEY")
        self.azure_openai_realtime_deployment: str = (
            e("AZURE_OPENAI_REALTIME_DEPLOYMENT") or "gpt-realtime-mini"
        )
        self._acs_callback_token = e("ACS_CALLBACK_TOKEN") or secrets.token_urlsafe(32)

        self.acs_resource_id: str = self._derive_acs_resource_id()

        self.admin_secret: str = e("ADMIN_SECRET")

        self.memory_model: str = e("MEMORY_MODEL") or "claude-sonnet-4.6"
        self.memory_idle_minutes: int = int(e("MEMORY_IDLE_MINUTES") or "5")
        self.proactive_enabled: bool = e("PROACTIVE_ENABLED").lower() in ("1", "true", "yes") if e("PROACTIVE_ENABLED") else False

        self.runtime_sp_app_id: str = e("RUNTIME_SP_APP_ID")
        self.runtime_sp_password: str = e("RUNTIME_SP_PASSWORD")
        self.runtime_sp_tenant: str = e("RUNTIME_SP_TENANT")

        self.aca_runtime_fqdn: str = e("ACA_RUNTIME_FQDN")
        self.aca_acr_name: str = e("ACA_ACR_NAME")
        self.aca_env_name: str = e("ACA_ENV_NAME")
        self.aca_storage_account: str = e("ACA_STORAGE_ACCOUNT")
        self.aca_mi_resource_id: str = e("ACA_MI_RESOURCE_ID")
        self.aca_mi_client_id: str = e("ACA_MI_CLIENT_ID")

        raw_wl = e("TELEGRAM_WHITELIST")
        self.telegram_whitelist: frozenset[str] = frozenset(
            uid.strip() for uid in raw_wl.split(",") if uid.strip()
        ) if raw_wl else frozenset()

    @property
    def data_dir(self) -> Path:
        return Path(os.getenv(self._DATA_DIR_ENV, str(Path.home() / ".polyclaw")))

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def memory_daily_dir(self) -> Path:
        return self.memory_dir / "daily"

    @property
    def memory_topics_dir(self) -> Path:
        return self.memory_dir / "topics"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def user_skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def builtin_skills_dir(self) -> Path:
        return self.project_root / "skills"

    @property
    def plugins_dir(self) -> Path:
        return self.project_root / "plugins"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def media_incoming_dir(self) -> Path:
        return self.media_dir / "incoming"

    @property
    def media_outgoing_dir(self) -> Path:
        return self.media_dir / "outgoing"

    @property
    def media_outgoing_pending_dir(self) -> Path:
        return self.media_outgoing_dir / "pending"

    @property
    def media_outgoing_sent_dir(self) -> Path:
        return self.media_outgoing_dir / "sent"

    @property
    def media_outgoing_error_dir(self) -> Path:
        return self.media_outgoing_dir / "error"

    @property
    def project_root(self) -> Path:
        env_root = os.getenv("POLYCLAW_PROJECT_ROOT")
        if env_root:
            return Path(env_root)
        p = Path(__file__).resolve().parent
        for _ in range(6):
            p = p.parent
            if (p / "plugins").is_dir() or (p / "pyproject.toml").is_file():
                return p
        return Path(__file__).resolve().parent.parent.parent.parent

    @property
    def soul_path(self) -> Path:
        return self.data_dir / "SOUL.md"

    @property
    def conversation_refs_path(self) -> Path:
        return self.data_dir / "conversation_refs.json"

    @property
    def scheduler_db_path(self) -> Path:
        return self.data_dir / "scheduler.json"

    @property
    def acs_callback_path(self) -> str:
        return "/api/voice/acs-callback"

    @property
    def acs_media_streaming_websocket_path(self) -> str:
        return "/api/voice/media-streaming"

    @property
    def acs_callback_token(self) -> str:
        return self._acs_callback_token

    def _read(self, key: str) -> str:
        raw = self.env.read(key) or os.getenv(key, "")
        if raw and key in SECRET_ENV_KEYS:
            from ..services.keyvault import resolve_if_kv_ref
            return resolve_if_kv_ref(raw)
        return raw

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.media_dir,
            self.media_incoming_dir,
            self.media_outgoing_pending_dir,
            self.media_outgoing_sent_dir,
            self.media_outgoing_error_dir,
            self.memory_dir,
            self.memory_daily_dir,
            self.memory_topics_dir,
            self.skills_dir,
            self.sessions_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def write_env(self, **kwargs: str) -> None:
        from ..services.keyvault import kv, env_key_to_secret_name, is_kv_ref

        secured = dict(kwargs)
        if kv.enabled:
            for key, value in kwargs.items():
                if key in _BOOTSTRAP_PLAINTEXT_KEYS:
                    continue
                if key in SECRET_ENV_KEYS and value and not is_kv_ref(value):
                    try:
                        secured[key] = kv.store(env_key_to_secret_name(key), value)
                    except Exception:
                        import logging
                        logging.getLogger(__name__).warning(
                            "Failed to store %s in Key Vault; writing plaintext", key,
                            exc_info=True,
                        )
        self.env.write(**secured)
        self.reload()

    def _derive_acs_resource_id(self) -> str:
        try:
            from ..realtime.auth import get_learned_audience
            learned = get_learned_audience()
            if learned:
                return learned
        except Exception:
            pass
        return ""


cfg = Settings()


def _reset_cfg() -> None:
    global cfg
    cfg = Settings()


register_singleton(_reset_cfg)
