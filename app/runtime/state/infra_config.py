"""Infrastructure configuration store -- bot, channels, voice."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from ._base import BaseConfigStore

logger = logging.getLogger(__name__)


@dataclass
class BotInfraConfig:
    resource_group: str = "polyclaw-rg"
    location: str = "eastus"
    display_name: str = "polyclaw"
    bot_handle: str = ""


@dataclass
class TelegramChannelConfig:
    token: str = ""
    whitelist: str = ""


@dataclass
class VoiceCallConfig:
    acs_resource_name: str = ""
    acs_connection_string: str = ""
    acs_source_number: str = ""
    voice_target_number: str = ""
    azure_openai_resource_name: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_realtime_deployment: str = ""
    resource_group: str = ""
    voice_resource_group: str = ""
    location: str = ""


@dataclass
class ChannelsConfig:
    telegram: TelegramChannelConfig = field(default_factory=TelegramChannelConfig)
    voice_call: VoiceCallConfig = field(default_factory=VoiceCallConfig)


@dataclass
class InfraConfig:
    """Top-level config dataclass wrapping bot and channel configs."""

    bot: BotInfraConfig = field(default_factory=BotInfraConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)


class InfraConfigStore(BaseConfigStore[InfraConfig]):
    """Persists infrastructure configuration to ``infra.json``."""

    _config_type = InfraConfig
    _default_filename = "infra.json"
    _log_label = "infra config"
    _SECRET_FIELDS = frozenset({"token", "acs_connection_string", "azure_openai_api_key"})
    _secret_prefix = "infra-"

    @property
    def bot(self) -> BotInfraConfig:
        return self._config.bot

    @property
    def channels(self) -> ChannelsConfig:
        return self._config.channels

    @property
    def bot_configured(self) -> bool:
        return bool(self.bot.resource_group and self.bot.location)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.channels.telegram.token)

    @property
    def voice_call_configured(self) -> bool:
        return bool(self.channels.voice_call.acs_connection_string)

    def _apply_raw(self, raw: dict[str, Any]) -> None:
        bot_data = raw.get("bot", {})
        for k, v in bot_data.items():
            if hasattr(self._config.bot, k):
                try:
                    setattr(self._config.bot, k, self._resolve_secret(v))
                except Exception:
                    logger.warning("Failed to resolve bot.%s -- skipping", k, exc_info=True)
        tg_data = raw.get("channels", {}).get("telegram", {})
        for k, v in tg_data.items():
            if hasattr(self._config.channels.telegram, k):
                try:
                    setattr(self._config.channels.telegram, k, self._resolve_secret(v))
                except Exception:
                    logger.warning("Failed to resolve telegram.%s -- skipping", k, exc_info=True)
        vc_data = raw.get("channels", {}).get("voice_call", {})
        for k, v in vc_data.items():
            if hasattr(self._config.channels.voice_call, k):
                try:
                    setattr(self._config.channels.voice_call, k, self._resolve_secret(v))
                except Exception:
                    logger.warning("Failed to resolve voice_call.%s -- skipping", k, exc_info=True)

    def _save_data(self) -> dict[str, Any]:
        return {
            "bot": asdict(self._config.bot),
            "channels": {
                "telegram": self._store_secrets(asdict(self._config.channels.telegram)),
                "voice_call": self._store_secrets(asdict(self._config.channels.voice_call)),
            },
        }

    def save_bot(self, **kwargs: str) -> None:
        for k, v in kwargs.items():
            if hasattr(self._config.bot, k):
                setattr(self._config.bot, k, v)
        self._save()

    def save_telegram(self, **kwargs: str) -> None:
        for k, v in kwargs.items():
            if hasattr(self._config.channels.telegram, k):
                setattr(self._config.channels.telegram, k, v)
        self._save()

    def clear_telegram(self) -> None:
        self._config.channels.telegram = TelegramChannelConfig()
        self._save()

    def save_voice_call(self, **kwargs: str) -> None:
        for k, v in kwargs.items():
            if hasattr(self._config.channels.voice_call, k):
                setattr(self._config.channels.voice_call, k, v)
        self._save()

    def clear_voice_call(self) -> None:
        self._config.channels.voice_call = VoiceCallConfig()
        self._save()

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "bot": asdict(self._config.bot),
            "channels": {
                "telegram": self._mask_secrets(asdict(self._config.channels.telegram)),
                "voice_call": self._mask_secrets(asdict(self._config.channels.voice_call)),
            },
        }

    def _mask_secrets(self, d: dict[str, Any]) -> dict[str, Any]:
        return {
            k: ("****" if k in self._SECRET_FIELDS and v else v)
            for k, v in d.items()
        }
