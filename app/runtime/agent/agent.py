"""Core agent -- wraps the GitHub Copilot SDK into a session manager."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from time import monotonic as _now
from typing import Any

from copilot import CopilotClient

from ..config.settings import cfg
from ..sandbox import SandboxExecutor, SandboxToolInterceptor
from ..services.otel import invoke_agent_span, set_span_attribute
from ..state.guardrails_config import GuardrailsConfigStore
from ..state.mcp_config import McpConfigStore
from .event_handler import EventHandler
from .hitl import HitlInterceptor
from .one_shot import auto_approve
from .prompt import build_system_prompt
from .tools import get_all_tools

logger = logging.getLogger(__name__)

MAX_START_RETRIES = 3
SESSION_TIMEOUT = 60
# Must be >= _APPROVAL_TIMEOUT in hitl.py (300s) so the agent doesn't
# abort the session while HITL is still waiting for human approval.
RESPONSE_TIMEOUT = 360.0
RETRY_DELAY = 2


class Agent:
    """Manages a CopilotClient + session lifecycle."""

    def __init__(self) -> None:
        self._client: CopilotClient | None = None
        self._session: Any = None
        self._authenticated: bool = False
        self.request_counts: dict[str, int] = {}
        self._sandbox: SandboxExecutor | None = None
        self._interceptor: SandboxToolInterceptor | None = None
        self._guardrails: GuardrailsConfigStore | None = None
        self._hitl: HitlInterceptor | None = None

    def set_sandbox(self, executor: SandboxExecutor) -> None:
        self._sandbox = executor
        self._interceptor = SandboxToolInterceptor(executor)

    def set_guardrails(self, guardrails: GuardrailsConfigStore) -> None:
        self._guardrails = guardrails
        self._hitl = HitlInterceptor(guardrails)

    @property
    def hitl_interceptor(self) -> HitlInterceptor | None:
        return self._hitl

    @property
    def has_session(self) -> bool:
        return self._session is not None

    async def start(self) -> None:
        cfg.ensure_dirs()
        opts: dict[str, Any] = {"log_level": "error"}
        if cfg.github_token:
            opts["github_token"] = cfg.github_token
            logger.info("[agent.start] GITHUB_TOKEN provided (%d chars)", len(cfg.github_token))
        else:
            logger.warning(
                "[agent.start] No GITHUB_TOKEN found -- Copilot CLI will try the "
                "logged-in gh session (may fail in containers)"
            )

        for attempt in range(1, MAX_START_RETRIES + 1):
            try:
                logger.info("[agent.start] attempt %d/%d -- creating CopilotClient", attempt, MAX_START_RETRIES)
                self._client = CopilotClient(opts)
                logger.info("[agent.start] calling client.start() ...")
                await self._client.start()
                logger.info("[agent.start] Copilot CLI started successfully")
                break
            except TimeoutError as exc:
                if attempt < MAX_START_RETRIES:
                    logger.warning("Copilot CLI startup timed out (attempt %d/%d)", attempt, MAX_START_RETRIES)
                    await self._safe_stop_client()
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    raise RuntimeError(
                        f"Could not connect to Copilot CLI after {MAX_START_RETRIES} attempts."
                    ) from exc

        # Start monitoring CLI stderr for hidden errors.
        self._start_stderr_monitor()

        # Verify the CLI is actually authenticated before accepting prompts.
        await self._verify_auth()

        # Validate the configured model is available.
        await self._verify_model()

    async def stop(self) -> None:
        await self._safe_destroy_session()
        await self._safe_stop_client()

    async def reload_auth(self) -> dict[str, Any]:
        """Reload GITHUB_TOKEN from ``.env`` and restart the Copilot client.

        Called by the ``/api/runtime/reload-auth`` endpoint when the admin
        container writes a new token to ``/data/.env`` after the runtime has
        already booted.
        """
        old_token = cfg.github_token
        cfg.reload()
        new_token = cfg.github_token

        if not new_token:
            return {"status": "no_token", "authenticated": False}

        if new_token == old_token and self._authenticated:
            return {"status": "unchanged", "authenticated": True}

        logger.info(
            "[agent.reload_auth] GITHUB_TOKEN changed (%d chars), restarting Copilot client ...",
            len(new_token),
        )
        await self.stop()
        await self.start()

        return {
            "status": "ok" if self._authenticated else "auth_failed",
            "authenticated": self._authenticated,
        }

    async def _verify_auth(self) -> None:
        """Check that the Copilot CLI is authenticated and log the result.

        Sets ``_authenticated`` so that :meth:`send` can fail fast with a
        useful error message instead of silently hanging for 120 seconds.
        """
        if not self._client:
            return
        try:
            auth = await self._client.get_auth_status()
            if auth.isAuthenticated:
                logger.info("[agent.auth] authenticated as %s", auth.login or "unknown")
                self._authenticated = True
            else:
                logger.error(
                    "[agent.auth] Copilot CLI is NOT authenticated. "
                    "Chat will not work. Set GITHUB_TOKEN in /data/.env "
                    "or use the admin setup wizard to authenticate."
                )
        except Exception:
            # auth.getStatus may not be supported on older CLI versions;
            # assume OK and let send() surface any real error.
            logger.debug("[agent.auth] auth status check unavailable", exc_info=True)
            self._authenticated = True  # optimistic

    async def _verify_model(self) -> None:
        """Log whether the configured model is available and enabled."""
        if not self._client:
            return
        model_id = cfg.copilot_model
        try:
            models = await self._client.list_models()
            available_ids = [m.id for m in models]
            match = next((m for m in models if m.id == model_id), None)
            if match:
                policy = match.policy.state if match.policy else "unknown"
                if policy == "enabled":
                    logger.info("[agent.model] model %s is available (policy=enabled)", model_id)
                else:
                    logger.warning(
                        "[agent.model] model %s found but policy=%s -- "
                        "requests may fail silently. Change COPILOT_MODEL in .env",
                        model_id, policy,
                    )
            else:
                logger.warning(
                    "[agent.model] model %s NOT found in %d available models: %s. "
                    "Requests may fail silently. Change COPILOT_MODEL in .env",
                    model_id, len(available_ids), available_ids[:10],
                )
        except Exception:
            logger.debug("[agent.model] could not list models", exc_info=True)

    def _start_stderr_monitor(self) -> None:
        """Read the Copilot CLI subprocess stderr in a daemon thread.

        The SDK pipes stderr but never reads it, so auth failures, rate
        limits, and API errors are completely invisible.  This drains and
        logs every line at WARNING level.
        """
        proc = getattr(self._client, "_process", None)
        if not proc:
            return
        stderr = getattr(proc, "stderr", None)
        if not stderr:
            return

        def _drain() -> None:
            try:
                for raw in stderr:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        logger.warning("[copilot-cli.stderr] %s", line)
            except Exception:
                pass  # stream closed

        t = threading.Thread(target=_drain, daemon=True, name="copilot-stderr")
        t.start()

    async def new_session(self) -> Any:
        if not self._client:
            raise RuntimeError("Agent not started")
        logger.info("[agent.new_session] destroying old session ...")
        await self._safe_destroy_session()
        session_cfg = self._build_session_config()
        logger.info(
            "[agent.new_session] creating session: model=%s, tools=%d, mcp_servers=%s",
            session_cfg.get("model"),
            len(session_cfg.get("tools", [])),
            list(session_cfg.get("mcp_servers", {}).keys()) if isinstance(session_cfg.get("mcp_servers"), dict) else "N/A",
        )
        self._session = await self._client.create_session(session_cfg)
        logger.info("[agent.new_session] session created: %s", type(self._session).__name__)
        return self._session

    async def send(
        self,
        prompt: str,
        on_delta: Callable[[str], None] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> str | None:
        logger.info("[agent.send] prompt=%r (len=%d), has_session=%s", prompt[:80], len(prompt), self._session is not None)

        if not self._authenticated:
            msg = (
                "Not authenticated. Please authenticate first.\n\n"
                "Open the setup wizard and either:\n"
                "- Sign in with GitHub, or\n"
                "- Paste a GitHub personal access token."
            )
            logger.error("[agent.send] aborting -- Copilot CLI not authenticated")
            if on_delta:
                on_delta(msg)
            return msg

        if not self._session:
            logger.info("[agent.send] no session -- creating one")
            await self.new_session()

        model = cfg.copilot_model
        self.request_counts[model] = self.request_counts.get(model, 0) + 1

        with invoke_agent_span("polyclaw", model=model) as span:
            return await self._send_inner(prompt, on_delta, on_event, span)

    async def _send_inner(
        self,
        prompt: str,
        on_delta: Callable[[str], None] | None,
        on_event: Callable[[str, dict], None] | None,
        otel_span: object | None,
    ) -> str | None:
        """Execute the actual send, wrapped by :meth:`send`'s OTel span."""
        handler = EventHandler(on_delta, on_event)
        unsub = self._session.on(handler)
        try:
            try:
                logger.info("[agent.send] calling session.send() ...")
                await self._session.send({"prompt": prompt})
                logger.info("[agent.send] session.send() returned, waiting for completion ...")
            except Exception as exc:
                logger.error("[agent.send] session.send() raised: %s", exc, exc_info=True)
                if "Session not found" in str(exc):
                    self._safe_unsub(unsub)
                    logger.info("[agent.send] session expired, creating new session...")
                    await self.new_session()
                    handler = EventHandler(on_delta, on_event)
                    unsub = self._session.on(handler)
                    await self._session.send({"prompt": prompt})
                else:
                    raise
            try:
                t0 = _now()
                await asyncio.wait_for(handler.done.wait(), timeout=RESPONSE_TIMEOUT)
                elapsed = _now() - t0
                logger.info(
                    "[agent.send] response complete in %.1fs, text_len=%d",
                    elapsed, len(handler.final_text or ""),
                )
            except TimeoutError:
                elapsed = _now() - t0
                logger.warning(
                    "[agent.send] response timed out after %.1fs (limit=%ss), "
                    "partial_len=%d, events_received=%d -- destroying stuck session",
                    elapsed, RESPONSE_TIMEOUT,
                    len(handler.final_text or ""), handler.event_count,
                )
                await self._abort_and_destroy_session()
                self._set_token_attributes(otel_span, handler)
                return handler.final_text
        except asyncio.CancelledError:
            logger.info(
                "[agent.send] cancelled -- aborting and destroying session "
                "to prevent stale state"
            )
            await self._abort_and_destroy_session()
            raise
        finally:
            self._safe_unsub(unsub)

        self._set_token_attributes(otel_span, handler)

        if handler.error:
            logger.error("[agent.send] session error: %s", handler.error)
            set_span_attribute("error.type", "SessionError")
            return None
        return handler.final_text

    @staticmethod
    def _set_token_attributes(span: object | None, handler: EventHandler) -> None:
        """Publish token usage from the event handler onto the OTel span."""
        if not span or not hasattr(span, "set_attribute"):
            return
        if handler.input_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", handler.input_tokens)  # type: ignore[union-attr]
        if handler.output_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", handler.output_tokens)  # type: ignore[union-attr]
        response_len = len(handler.final_text or "")
        span.set_attribute("gen_ai.response.length", response_len)  # type: ignore[union-attr]

    async def list_models(self) -> list[dict]:
        if not self._client:
            raise RuntimeError("Agent not started")
        try:
            models = await self._client.list_models()
            return [
                {
                    "id": m.id,
                    "name": m.name,
                    "policy": m.policy.state if m.policy else "enabled",
                    "billing_multiplier": m.billing.multiplier if m.billing else 1.0,
                    "reasoning_efforts": m.supported_reasoning_efforts,
                }
                for m in models
            ]
        except Exception as exc:
            logger.warning("Failed to list models: %s", exc)
            return []

    def _build_session_config(self) -> dict[str, Any]:
        sandbox_active = self._interceptor and self._sandbox and self._sandbox.enabled
        # Always register the HITL hook when an interceptor exists so that
        # guardrails config changes (enable/disable) take effect without
        # requiring a session restart.  The hook itself checks hitl_enabled
        # at call time via resolve_action() which returns "allow" when off.
        hitl_available = self._hitl is not None

        logger.info(
            "[agent.config] building session config: "
            "sandbox_active=%s hitl_available=%s hitl_enabled=%s",
            sandbox_active, hitl_available,
            self._guardrails.hitl_enabled if self._guardrails else "(no store)",
        )

        if sandbox_active and hitl_available:
            hitl = self._hitl
            sandbox = self._interceptor

            async def chained_pre_tool_use(input_data: dict, invocation: Any) -> dict:
                logger.info(
                    "[agent.hook] chained_pre_tool_use called: tool=%s",
                    input_data.get("toolName", "?"),
                )
                result = await hitl.on_pre_tool_use(input_data, invocation)
                if result.get("permissionDecision") != "allow":
                    logger.info("[agent.hook] hitl denied, skipping sandbox")
                    return result
                logger.info("[agent.hook] hitl allowed, proceeding to sandbox")
                return await sandbox.on_pre_tool_use(input_data, invocation)

            hooks: dict[str, Any] = {
                "on_pre_tool_use": chained_pre_tool_use,
                "on_post_tool_use": sandbox.on_post_tool_use,
            }
            logger.info("[agent.config] hooks: chained (hitl + sandbox)")
        elif hitl_available:
            hooks = {"on_pre_tool_use": self._hitl.on_pre_tool_use}
            logger.info("[agent.config] hooks: hitl only")
        elif sandbox_active:
            hooks = {
                "on_pre_tool_use": self._interceptor.on_pre_tool_use,
                "on_post_tool_use": self._interceptor.on_post_tool_use,
            }
            logger.info("[agent.config] hooks: sandbox only")
        else:
            hooks = {"on_pre_tool_use": auto_approve}
            logger.info("[agent.config] hooks: auto_approve (no hitl, no sandbox)")

        session_cfg: dict[str, Any] = {
            "model": cfg.copilot_model,
            "streaming": True,
            "tools": get_all_tools(),
            "system_message": {"mode": "replace", "content": build_system_prompt()},
            "hooks": hooks,
            "skill_directories": [str(cfg.builtin_skills_dir), str(cfg.user_skills_dir)],
        }

        if sandbox_active:
            session_cfg["excluded_tools"] = ["create", "view", "edit", "grep", "glob"]

        try:
            session_cfg["mcp_servers"] = McpConfigStore().get_enabled_servers()
        except Exception:
            logger.warning("Failed to load MCP config, using defaults", exc_info=True)
            session_cfg["mcp_servers"] = {
                "playwright": {
                    "type": "local",
                    "command": "npx",
                    "args": ["-y", "@playwright/mcp@latest", "--browser", "chromium", "--headless", "--isolated"],
                    "env": {"PLAYWRIGHT_CHROMIUM_ARGS": "--no-sandbox --disable-setuid-sandbox"},
                    "tools": ["*"],
                },
            }
        return session_cfg

    async def _abort_and_destroy_session(self) -> None:
        """Abort any in-flight request, then destroy the session.

        Used after timeouts and cancellations to ensure the next ``send()``
        gets a clean session instead of reusing one stuck on a pending
        model request.
        """
        if self._session:
            try:
                await self._session.abort()
                logger.info("[agent] session aborted")
            except Exception:
                logger.debug("Error aborting session", exc_info=True)
        await self._safe_destroy_session()

    async def _safe_destroy_session(self) -> None:
        if self._session:
            try:
                await self._session.destroy()
            except Exception:
                logger.debug("Error destroying session", exc_info=True)
            self._session = None

    @staticmethod
    def _safe_unsub(unsub: Callable[[], None]) -> None:
        """Call *unsub* without raising if the session was already destroyed."""
        try:
            unsub()
        except Exception:  # noqa: BLE001
            pass

    async def _safe_stop_client(self) -> None:
        if self._client:
            try:
                await self._client.stop()
            except Exception:
                logger.debug("Error stopping client", exc_info=True)
            self._client = None
