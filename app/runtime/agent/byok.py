"""BYOK provider configuration for the Copilot SDK."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from ..config.settings import cfg

logger = logging.getLogger(__name__)

_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com"


def get_bearer_token() -> str:
    """Obtain a short-lived Entra ID token for Cognitive Services."""
    try:
        result = subprocess.run(
            [
                "az", "account", "get-access-token",
                "--resource", _COGNITIVE_SERVICES_SCOPE,
                "--query", "accessToken",
                "--output", "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error("[byok] az get-access-token failed: %s", result.stderr.strip())
            return ""
        return json.loads(result.stdout)
    except Exception:
        logger.error("[byok] failed to obtain bearer token", exc_info=True)
        return ""


def build_provider_config() -> dict[str, Any] | None:
    """Build the BYOK provider dict for a Copilot SDK session."""
    endpoint = cfg.foundry_endpoint
    if not endpoint:
        return None

    token = get_bearer_token()
    if not token:
        logger.warning("[byok] no bearer token -- Foundry BYOK will not work")
        return None

    return {
        "type": "azure",
        "base_url": endpoint.rstrip("/"),
        "bearer_token": token,
        "azure": {"api_version": "2024-10-21"},
    }


def build_session_overrides() -> dict[str, Any]:
    """Return extra session config kwargs when BYOK is active."""
    provider = build_provider_config()
    if provider is None:
        return {}

    return {
        "model": cfg.copilot_model,
        "provider": provider,
    }
