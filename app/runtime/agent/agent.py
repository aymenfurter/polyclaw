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
from ..state.guardrails import GuardrailsConfigStore
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
        self._byok: bool = False
        self.request_counts: dict[str, int] = {}
        self._sandbox: SandboxExecutor | None = None
        self._interceptor: SandboxToolInterceptor | None = None
        self._guardrails: GuardrailsConfigStore | None = None
        self._hitl: HitlInterceptor | None = None
        self._send_lock = asyncio.Lock()

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

        # Auth is handled per-session via the Foundry BYOK provider block.
        self._byok = bool(cfg.foundry_endpoint)

        if self._byok:
            logger.info("[agent.start] Foundry BYOK mode")
        else:
            logger.warning(
                "[agent.start] No FOUNDRY_ENDPOINT -- authentication may fail"
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
        """Reload configuration and restart the Copilot client."""
        old_endpoint = cfg.foundry_endpoint
        cfg.reload()
        new_endpoint = cfg.foundry_endpoint

        endpoint_changed = new_endpoint != old_endpoint

        if not endpoint_changed and self._authenticated:
            return {"status": "unchanged", "authenticated": True}

        if not new_endpoint:
            return {"status": "no_auth", "authenticated": False}

        logger.info(
            "[agent.reload_auth] config changed (endpoint=%s), "
            "restarting Copilot client ...",
            "changed" if endpoint_changed else "same",
        )
        await self.stop()
        await self.start()

        return {
            "status": "ok" if self._authenticated else "auth_failed",
            "authenticated": self._authenticated,
            "byok": bool(new_endpoint),
        }

    async def _verify_auth(self) -> None:
        """Check that the Copilot CLI is authenticated."""
        if not self._client:
            return

        if self._byok:
            logger.info("[agent.auth] BYOK mode -- skipping GitHub auth check")
            self._authenticated = True
            return

        try:
            auth = await self._client.get_auth_status()
            if auth.isAuthenticated:
                logger.info("[agent.auth] authenticated as %s", auth.login or "unknown")
                self._authenticated = True
            else:
                logger.error(
                    "[agent.auth] Copilot CLI is NOT authenticated. "
                    "Chat will not work. Configure FOUNDRY_ENDPOINT "
                    "for Foundry BYOK mode."
                )
        except Exception:
            # auth.getStatus may not be supported on older CLI versions.
            logger.debug("[agent.auth] auth status check unavailable", exc_info=True)
            self._authenticated = True

    async def _verify_model(self) -> None:
        """Log whether the configured model is available and enabled."""
        if not self._client:
            return
        if self._byok:
            logger.info("[agent.model] BYOK mode -- using Foundry model %s", cfg.copilot_model)
            return
        model_id = cfg.copilot_model
        try:
            models = await self._client.list_models()
            match = next((m for m in models if m.id == model_id), None)
            if match:
                policy = match.policy.state if match.policy else "unknown"
                if policy != "enabled":
                    logger.warning(
                        "[agent.model] model %s found but policy=%s -- "
                        "requests may fail silently. Change COPILOT_MODEL in .env",
                        model_id, policy,
                    )
                else:
                    logger.info("[agent.model] model %s is available (policy=enabled)", model_id)
            else:
                available_ids = [m.id for m in models]
                logger.warning(
                    "[agent.model] model %s NOT found in %d available models: %s",
                    model_id, len(available_ids), available_ids[:10],
                )
        except Exception:
            logger.debug("[agent.model] could not list models", exc_info=True)

    def _start_stderr_monitor(self) -> None:
        """Drain Copilot CLI stderr in a daemon thread and log at WARNING."""
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
        async with self._send_lock:
            return await self._new_session_inner()

    async def _new_session_inner(self) -> Any:
        """Create a new SDK session. Caller must hold ``_send_lock``."""
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

    async def ensure_session(self) -> Any:
        """Return the existing SDK session, or create one if none exists."""
        if self._session:
            return self._session
        if not self._client:
            raise RuntimeError("Agent not started")
        async with self._send_lock:
            if self._session:
                return self._session
            return await self._new_session_inner()

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
                "Open the setup wizard and deploy Foundry infrastructure."
            )
            logger.error("[agent.send] aborting -- Copilot CLI not authenticated")
            if on_delta:
                on_delta(msg)
            return msg

        model = cfg.copilot_model
        self.request_counts[model] = self.request_counts.get(model, 0) + 1

        t_lock_wait = _now()
        logger.info("[agent.send] waiting for send lock ...")
        async with self._send_lock:
            t_lock_acq = _now()
            logger.info(
                "[agent.send] send lock acquired (waited %.0fms)",
                (t_lock_acq - t_lock_wait) * 1000,
            )
            if not self._session:
                logger.info("[agent.send] no session -- creating one")
                await self._new_session_inner()
            with invoke_agent_span("polyclaw", model=model) as span:
                return await self._send_inner(prompt, on_delta, on_event, span)

    async def _send_inner(
        self,
        prompt: str,
        on_delta: Callable[[str], None] | None,
        on_event: Callable[[str, dict], None] | None,
        otel_span: object | None,
    ) -> str | None:
        """Execute the actual send within the OTel span."""
        handler = EventHandler(on_delta, on_event)
        unsub = self._session.on(handler)
        try:
            try:
                t_sdk = _now()
                logger.info("[agent.send] calling session.send() ...")
                await self._session.send({"prompt": prompt})
                logger.info(
                    "[agent.send] session.send() returned in %.0fms, waiting for completion ...",
                    (_now() - t_sdk) * 1000,
                )
            except Exception as exc:
                logger.error("[agent.send] session.send() raised: %s", exc, exc_info=True)
                if "Session not found" in str(exc):
                    self._safe_unsub(unsub)
                    logger.info("[agent.send] session expired, creating new session...")
                    await self._new_session_inner()
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

        # BYOK mode: return Foundry-deployed models from .env
        if self._byok:
            return self._list_foundry_models()

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

    @staticmethod
    def _list_foundry_models() -> list[dict]:
        """Return models deployed on the Foundry endpoint."""
        raw = cfg.env.read("DEPLOYED_MODELS") or ""
        names = [n.strip() for n in raw.split(",") if n.strip()] if raw else []
        if not names:
            names = [cfg.copilot_model]
        return [
            {
                "id": name,
                "name": name,
                "policy": "enabled",
                "billing_multiplier": 1.0,
                "reasoning_efforts": [],
            }
            for name in names
        ]

    def _build_hooks(self) -> dict[str, Any]:
        """Compose pre/post-tool-use hooks from active interceptors."""
        sandbox_active = (
            self._interceptor and self._sandbox and self._sandbox.enabled
        )
        hitl_available = self._hitl is not None

        if sandbox_active and hitl_available:
            hitl = self._hitl
            sandbox = self._interceptor

            async def chained_pre_tool_use(
                input_data: dict, invocation: Any,
            ) -> dict:
                logger.info(
                    "[agent.hook] chained_pre_tool_use called: tool=%s",
                    input_data.get("toolName", "?"),
                )
                result = await hitl.on_pre_tool_use(input_data, invocation)
                if result.get("permissionDecision") != "allow":
                    logger.info("[agent.hook] hitl denied, skipping sandbox")
                    return result
                logger.info(
                    "[agent.hook] hitl allowed, proceeding to sandbox",
                )
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
            logger.info(
                "[agent.config] hooks: auto_approve (no hitl, no sandbox)",
            )

        return hooks

    def _build_session_config(self) -> dict[str, Any]:
        """Assemble the full session configuration for the Copilot SDK."""
        sandbox_active = (
            self._interceptor and self._sandbox and self._sandbox.enabled
        )
        logger.info(
            "[agent.config] building session config: "
            "sandbox_active=%s hitl_available=%s hitl_enabled=%s",
            sandbox_active, self._hitl is not None,
            self._guardrails.hitl_enabled if self._guardrails else "(no store)",
        )

        session_cfg: dict[str, Any] = {
            "model": cfg.copilot_model,
            "streaming": True,
            "tools": get_all_tools(),
            "system_message": {
                "mode": "replace",
                "content": build_system_prompt(),
            },
            "hooks": self._build_hooks(),
            "skill_directories": [
                str(cfg.builtin_skills_dir),
                str(cfg.user_skills_dir),
            ],
        }

        if sandbox_active:
            session_cfg["excluded_tools"] = [
                "create", "view", "edit", "grep", "glob",
            ]

        try:
            session_cfg["mcp_servers"] = (
                McpConfigStore().get_enabled_servers()
            )
        except Exception:
            logger.warning(
                "Failed to load MCP config, using defaults",
                exc_info=True,
            )
            session_cfg["mcp_servers"] = {
                "playwright": {
                    "type": "local",
                    "command": "npx",
                    "args": [
                        "-y", "@playwright/mcp@latest",
                        "--browser", "chromium",
                        "--headless", "--isolated",
                    ],
                    "env": {
                        "PLAYWRIGHT_CHROMIUM_ARGS":
                            "--no-sandbox --disable-setuid-sandbox",
                    },
                    "tools": ["*"],
                },
            }

        # Inject BYOK provider when Foundry is configured.
        if self._byok:
            from .byok import build_session_overrides

            overrides = build_session_overrides()
            if overrides:
                session_cfg.update(overrides)
                logger.info(
                    "[agent.config] BYOK provider injected: endpoint=%s model=%s",
                    cfg.foundry_endpoint, session_cfg.get("model"),
                )

        return session_cfg

    async def _abort_and_destroy_session(self) -> None:
        """Abort any in-flight request, then destroy the session."""
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
