"""Azure AI Content Safety Prompt Shields integration.

Calls the Prompt Shields API to detect prompt injection attacks in tool
arguments before execution.  No fallback -- the Content Safety endpoint
must be configured.

Authentication always uses Entra ID (``DefaultAzureCredential``) with
scope ``https://cognitiveservices.azure.com/.default``.  API keys are
never used -- Azure policies commonly enforce ``disableLocalAuth=true``
on Cognitive Services resources.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"

_API_VERSION = "2024-09-01"


@dataclass(frozen=True)
class ShieldResult:
    """Result of a Prompt Shields analysis."""

    attack_detected: bool
    mode: str  # always "prompt_shields"
    detail: str = ""


class PromptShieldService:
    """Client for Azure AI Content Safety Prompt Shields API.

    Only requires an ``endpoint``.  Authentication is handled
    automatically via ``DefaultAzureCredential`` (managed identity,
    Azure CLI, etc.).
    """

    def __init__(
        self,
        endpoint: str = "",
        mode: str = "prompt_shields",
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._mode = mode
        self._token_provider: _BearerTokenProvider | None = None
        logger.info(
            "[prompt_shield.init] endpoint=%s configured=%s",
            self._endpoint or "(none)", bool(self._endpoint),
        )

    @property
    def configured(self) -> bool:
        """True when the Prompt Shields endpoint is set."""
        return bool(self._endpoint)

    def update_config(
        self,
        *,
        endpoint: str | None = None,
        mode: str | None = None,
    ) -> None:
        """Update configuration at runtime."""
        if endpoint is not None:
            self._endpoint = endpoint.rstrip("/")
        if mode is not None:
            self._mode = mode
        logger.info(
            "[prompt_shield.config] updated endpoint=%s configured=%s",
            self._endpoint or "(none)", self.configured,
        )

    def check(self, text: str) -> ShieldResult:
        """Check text for prompt injection via Content Safety API.

        When no endpoint is configured the check is skipped and the text
        is treated as clean so that the absence of a Content Safety
        resource does not block normal operation.
        """
        if not self.configured:
            logger.warning(
                "[prompt_shield.check] no endpoint configured -- skipping check"
            )
            return ShieldResult(
                attack_detected=False,
                mode="prompt_shields",
                detail="Content Safety endpoint not configured -- check skipped",
            )
        logger.info(
            "[prompt_shield.check] scanning %d chars via Content Safety API",
            len(text),
        )
        return self._api_check(text)

    def _get_auth_header(self) -> dict[str, str]:
        """Return the Entra ID bearer token header for the API."""
        if self._token_provider is None:
            self._token_provider = _BearerTokenProvider()
        token = self._token_provider.get_token()
        logger.debug("[prompt_shield] using Entra ID bearer token")
        return {"Authorization": f"Bearer {token}"}

    def _api_check(self, text: str) -> ShieldResult:
        """Call the Prompt Shields REST API."""
        url = (
            f"{self._endpoint}/contentsafety/text:shieldPrompt"
            f"?api-version={_API_VERSION}"
        )
        body = json.dumps({"userPrompt": text, "documents": []}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(self._get_auth_header())
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                elapsed_ms = (time.monotonic() - t0) * 1000
                data = json.loads(raw)
            analysis = data.get("userPromptAnalysis", {})
            detected = analysis.get("attackDetected", False)
            detail = "Attack detected by Prompt Shields" if detected else "Clean"
            logger.info(
                "[prompt_shield.api] result=%s elapsed=%.0fms detail=%s",
                "ATTACK" if detected else "CLEAN", elapsed_ms, detail,
            )
            return ShieldResult(
                attack_detected=detected,
                mode="prompt_shields",
                detail=detail,
            )
        except urllib.error.HTTPError as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            body_text = exc.read().decode("utf-8", errors="replace")[:500]
            logger.error(
                "[prompt_shield.api] HTTP %s elapsed=%.0fms body=%s",
                exc.code, elapsed_ms, body_text,
            )
            # Any API error blocks the call -- no silent fallback.
            return ShieldResult(
                attack_detected=True,
                mode="prompt_shields",
                detail=f"Content Safety API error (HTTP {exc.code}) -- blocking for safety",
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error(
                "[prompt_shield.api] request failed elapsed=%.0fms error=%s",
                elapsed_ms, exc, exc_info=True,
            )
            return ShieldResult(
                attack_detected=True,
                mode="prompt_shields",
                detail=f"Content Safety API unreachable -- blocking for safety: {exc}",
            )

    def dry_run(self) -> ShieldResult:
        """Send a harmless probe to verify API connectivity and RBAC.

        Returns a ``ShieldResult`` whose ``attack_detected`` is ``False``
        when the API accepted the call (permissions OK) and ``True`` when
        auth or connectivity failed.  The ``detail`` field contains a
        human-readable explanation.
        """
        if not self.configured:
            return ShieldResult(
                attack_detected=True,
                mode="prompt_shields",
                detail="No endpoint configured",
            )

        url = (
            f"{self._endpoint}/contentsafety/text:shieldPrompt"
            f"?api-version={_API_VERSION}"
        )
        body = json.dumps(
            {"userPrompt": "Hello, this is a connectivity test.", "documents": []},
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        try:
            headers.update(self._get_auth_header())
        except Exception as exc:
            return ShieldResult(
                attack_detected=True,
                mode="prompt_shields",
                detail=f"Token acquisition failed: {exc}",
            )

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            logger.info("[prompt_shield.dry_run] API reachable, auth OK")
            return ShieldResult(
                attack_detected=False,
                mode="prompt_shields",
                detail="API reachable, auth OK",
            )
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:500]
            logger.error("[prompt_shield.dry_run] HTTP %s: %s", exc.code, body_text)
            return ShieldResult(
                attack_detected=True,
                mode="prompt_shields",
                detail=f"HTTP {exc.code}: {body_text}",
            )
        except Exception as exc:
            logger.error("[prompt_shield.dry_run] connection failed: %s", exc, exc_info=True)
            return ShieldResult(
                attack_detected=True,
                mode="prompt_shields",
                detail=f"Connection failed: {exc}",
            )


class _BearerTokenProvider:
    """Lazily acquire and cache bearer tokens via ``DefaultAzureCredential``."""

    def __init__(self) -> None:
        self._credential: object | None = None
        self._cached_token: str = ""
        self._expires_on: float = 0.0

    def get_token(self) -> str:
        """Return a valid bearer token, refreshing if necessary."""
        # Return cached token if still valid (with 5-min buffer)
        if self._cached_token and time.time() < self._expires_on - 300:
            return self._cached_token

        if self._credential is None:
            try:
                from azure.identity import DefaultAzureCredential
                self._credential = DefaultAzureCredential()
            except Exception as exc:
                logger.error("[prompt_shield.token] DefaultAzureCredential init failed: %s", exc)
                raise

        try:
            token = self._credential.get_token(_COGNITIVE_SCOPE)  # type: ignore[union-attr]
            self._cached_token = token.token
            self._expires_on = token.expires_on
            logger.info("[prompt_shield.token] acquired bearer token (expires in %.0fs)",
                        self._expires_on - time.time())
            return self._cached_token
        except Exception as exc:
            logger.error("[prompt_shield.token] failed to acquire token: %s", exc)
            raise
