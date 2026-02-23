"""WebSocket chat handler -- /api/chat/ws."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web

from ..config.settings import cfg
from ..media.outgoing import collect_pending_outgoing
from ..messaging.cards import attachment_to_dict, drain_pending_cards
from ..messaging.commands import CommandDispatcher
from ..services.otel import agent_span, record_event, set_span_attribute
from ..state.memory import get_memory
from ..state.session_store import SessionStore
from ..state.tool_activity_store import ToolActivityStore, get_tool_activity_store

if TYPE_CHECKING:
    from ..agent.agent import Agent
    from ..agent.hitl import HitlInterceptor
    from ..sandbox import SandboxToolInterceptor

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class ChatHandler:
    """WebSocket handler for the admin chat interface."""

    def __init__(
        self,
        agent: Agent,
        session_store: SessionStore,
        sandbox_interceptor: SandboxToolInterceptor | None = None,
        hitl_interceptor: HitlInterceptor | None = None,
        tool_activity_store: ToolActivityStore | None = None,
    ) -> None:
        self._agent = agent
        self._sessions = session_store
        self._sandbox = sandbox_interceptor
        self._hitl = hitl_interceptor
        self._tool_activity = tool_activity_store or get_tool_activity_store()
        self._commands = CommandDispatcher(agent, session_store=session_store)
        self._suggestions = self._load_suggestions()

    def register(self, router: web.UrlDispatcher) -> None:
        router.add_get("/api/chat/ws", self.handle)
        router.add_get("/api/models", self.list_models)
        router.add_get("/api/chat/models", self.list_models)
        router.add_get("/api/chat/suggestions", self.get_suggestions)

    async def handle(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        logger.info("[chat.handle] WebSocket connected from %s", req.remote)

        # Track the current send task so approve_tool can arrive while
        # agent.send() is blocked waiting for HITL approval.
        send_task: asyncio.Task[None] | None = None

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                logger.debug("[chat.handle] received: %s", msg.data[:200])
                try:
                    data = json.loads(msg.data)
                    action = data.get("action", "")
                    if action == "send":
                        if send_task and not send_task.done():
                            logger.warning("[chat.handle] send already in progress, ignoring")
                            continue
                        send_task = asyncio.create_task(self._dispatch(ws, data))
                    else:
                        await self._dispatch(ws, data)
                except json.JSONDecodeError:
                    logger.warning("[chat.handle] invalid JSON: %s", msg.data[:100])
                    await ws.send_json({"type": "error", "content": "Invalid JSON"})
                except Exception:
                    logger.exception("[chat.handle] unhandled error in dispatch")
                    await ws.send_json({"type": "error", "content": "Internal error"})
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("[chat.handle] WebSocket error: %s", ws.exception())

        # Cancel inflight send on disconnect
        if send_task and not send_task.done():
            send_task.cancel()
        logger.info("[chat.handle] WebSocket disconnected")
        return ws

    async def list_models(self, _req: web.Request) -> web.Response:
        try:
            models = await self._agent.list_models()
        except Exception:
            models = []
        return web.json_response({
            "models": models,
            "current": cfg.copilot_model,
        })

    async def get_suggestions(self, _req: web.Request) -> web.Response:
        return web.json_response({"suggestions": self._suggestions})

    async def _dispatch(self, ws: web.WebSocketResponse, data: dict) -> None:
        action = data.get("action", "")
        logger.info("[chat.dispatch] action=%s keys=%s", action, list(data.keys()))

        handler = self._ACTION_DISPATCH.get(action)
        if handler is not None:
            await handler(self, ws, data)
        else:
            logger.warning("[chat.dispatch] unknown action: %s", action)
            await ws.send_json({"type": "error", "content": f"Unknown action: {action}"})

    async def _handle_new_session(
        self, ws: web.WebSocketResponse, _data: dict
    ) -> None:
        await self._agent.new_session()
        session_id = str(uuid.uuid4())
        logger.info("[chat.dispatch] new session created: %s", session_id)
        self._sessions.start_session(session_id, model=cfg.copilot_model)
        await ws.send_json({"type": "session_created", "session_id": session_id})

    async def _dispatch_resume(
        self, ws: web.WebSocketResponse, data: dict
    ) -> None:
        await self._resume_session(ws, data.get("session_id", ""))

    async def _dispatch_send(
        self, ws: web.WebSocketResponse, data: dict
    ) -> None:
        await self._send_prompt(ws, data)

    async def _dispatch_approve(
        self, ws: web.WebSocketResponse, data: dict
    ) -> None:
        await self._handle_tool_approval(ws, data)

    _ACTION_DISPATCH: dict[str, Any] = {
        "new_session": _handle_new_session,
        "resume_session": _dispatch_resume,
        "send": _dispatch_send,
        "approve_tool": _dispatch_approve,
    }

    async def _send_prompt(self, ws: web.WebSocketResponse, data: dict) -> None:
        text = (data.get("text") or data.get("message") or "").strip()
        if not text:
            logger.debug("[chat.send_prompt] empty text, ignoring")
            return

        session_id = data.get("session_id", "")
        logger.info(
            "[chat.send_prompt] text=%r session=%s",
            text[:80], session_id or "(none)",
        )

        self._ensure_active_session(session_id)

        # Slash command dispatch
        if text.startswith("/"):
            handled = await self._try_command(ws, text, session_id)
            if handled:
                logger.info("[chat.send_prompt] handled as command")
                return

        self._sessions.record("user", text)
        memory = get_memory()
        memory.record("user", text)

        chunks: list[str] = []
        on_delta, on_event = self._make_event_callbacks(ws, chunks)
        self._bind_hitl(ws)

        logger.info("[chat.send_prompt] calling agent.send() ...")
        with agent_span(
            "chat.agent_turn",
            attributes={
                "chat.prompt_length": len(text),
                "chat.session_id": session_id or "",
            },
        ):
            try:
                response = await self._agent.send(
                    text,
                    on_delta=lambda d: asyncio.ensure_future(on_delta(d)),
                    on_event=lambda t, d: asyncio.ensure_future(
                        on_event({"type": t, **d}),
                    ),
                )
            except Exception:
                logger.exception("[chat.send_prompt] agent.send() raised")
                record_event("agent_error")
                await ws.send_json({
                    "type": "error",
                    "content": "Agent error -- check server logs",
                })
                return
            finally:
                self._unbind_hitl()
            full_text = "".join(chunks) or response or ""
            set_span_attribute("chat.response_length", len(full_text))
            set_span_attribute("chat.chunk_count", len(chunks))

        await self._finalize_response(ws, full_text, chunks, memory)

    # -- _send_prompt helpers ----------------------------------------------

    def _ensure_active_session(self, session_id: str) -> None:
        """Ensure the session store is tracking an active session."""
        if session_id and self._sessions.current_session_id != session_id:
            self._sessions.start_session(session_id)
        elif not self._sessions.current_session_id:
            auto_id = str(uuid.uuid4())
            logger.info(
                "[chat.send_prompt] no active session, auto-creating %s",
                auto_id,
            )
            self._sessions.start_session(auto_id, model=cfg.copilot_model)

    def _make_event_callbacks(
        self, ws: web.WebSocketResponse, chunks: list[str],
    ) -> tuple[
        Any,  # on_delta coroutine
        Any,  # on_event coroutine
    ]:
        """Build the delta and event callback coroutines for agent.send."""

        async def on_delta(delta: str) -> None:
            chunks.append(delta)
            await ws.send_json({"type": "delta", "content": delta})

        async def on_event(event: dict[str, Any]) -> None:
            event_type = event.pop("type", "")
            if event_type == "sandbox_exec" and self._sandbox:
                result = await self._sandbox.intercept(
                    {"type": event_type, **event},
                )
                if result:
                    await ws.send_json({"type": "sandbox_result", **result})
            # Record tool activity for audit
            if event_type == "tool_start":
                mcp_server = event.get("mcp_server", "")
                category = "mcp" if mcp_server else ""
                tool_name = event.get("tool", "unknown")
                interaction_type = ""
                if self._hitl:
                    interaction_type = (
                        self._hitl.pop_resolved_strategy(tool_name)
                    )
                self._tool_activity.record_start(
                    session_id=self._sessions.current_session_id,
                    tool=tool_name,
                    call_id=event.get("call_id", ""),
                    arguments=event.get("arguments", ""),
                    model=cfg.copilot_model,
                    category=category,
                    interaction_type=interaction_type,
                )
            elif event_type == "tool_done":
                self._tool_activity.record_complete(
                    call_id=event.get("call_id", ""),
                    result=event.get("result", ""),
                )
            await ws.send_json({
                "type": "event", "event": event_type, **event,
            })

        return on_delta, on_event

    def _bind_hitl(self, ws: web.WebSocketResponse) -> None:
        """Bind the HITL emitter so approval requests reach the WebSocket."""
        if not self._hitl:
            logger.info("[chat.send_prompt] no HITL interceptor available")
            return

        def hitl_emit(etype: str, payload: dict[str, Any]) -> None:
            logger.info(
                "[chat.hitl_emit] sending event=%s payload_keys=%s",
                etype, list(payload.keys()),
            )
            asyncio.ensure_future(
                ws.send_json({"type": "event", "event": etype, **payload}),
            )

        self._hitl.bind_turn(
            emit=hitl_emit,
            execution_context="interactive",
            model=cfg.copilot_model,
            tool_activity=self._tool_activity,
            session_id=self._sessions.current_session_id,
        )
        logger.info(
            "[chat.send_prompt] HITL emitter bound: model=%s",
            cfg.copilot_model,
        )

    def _unbind_hitl(self) -> None:
        """Clear the HITL emitter after a turn completes."""
        if self._hitl:
            self._hitl.unbind_turn()

    async def _finalize_response(
        self,
        ws: web.WebSocketResponse,
        full_text: str,
        chunks: list[str],
        memory: Any,
    ) -> None:
        """Log, persist, and send the final response artifacts."""
        logger.info(
            "[chat.send_prompt] response complete, len=%d, chunks=%d",
            len(full_text), len(chunks),
        )

        if not full_text:
            logger.warning(
                "[chat.send_prompt] empty response -- "
                "model may have timed out",
            )
            await ws.send_json({
                "type": "error",
                "content": (
                    "The model did not respond. "
                    "This can happen when the model is overloaded or "
                    "the session is stale. Please try again."
                ),
            })

        self._sessions.record("assistant", full_text)
        memory.record("assistant", full_text)

        outgoing = collect_pending_outgoing()
        cards = drain_pending_cards()

        if outgoing:
            await ws.send_json({"type": "media", "files": outgoing})
        if cards:
            await ws.send_json({
                "type": "cards",
                "cards": [attachment_to_dict(c) for c in cards],
            })
        await ws.send_json({"type": "done"})

    async def _try_command(
        self, ws: web.WebSocketResponse, text: str, session_id: str
    ) -> bool:
        if not text.startswith("/"):
            return False

        async def reply(content: str) -> None:
            await ws.send_json({"type": "message", "content": content})
            await ws.send_json({"type": "done"})

        return await self._commands.try_handle(text, reply, channel="web")

    async def _handle_tool_approval(
        self, ws: web.WebSocketResponse, data: dict
    ) -> None:
        """Handle an approve_tool action from the frontend."""
        call_id = data.get("call_id", "")
        response_text = (data.get("response") or "").strip().lower()
        approved = response_text in ("y", "yes")

        if not self._hitl:
            logger.warning("[chat.approve_tool] no HITL interceptor configured")
            await ws.send_json({"type": "event", "event": "approval_resolved", "call_id": call_id, "approved": False})
            return

        resolved = self._hitl.resolve_approval(call_id, approved)
        logger.info(
            "[chat.approve_tool] call_id=%s approved=%s resolved=%s",
            call_id, approved, resolved,
        )
        await ws.send_json({
            "type": "event",
            "event": "approval_resolved",
            "call_id": call_id,
            "approved": approved,
        })

    async def _resume_session(
        self, ws: web.WebSocketResponse, session_id: str
    ) -> None:
        session = self._sessions.get_session(session_id)
        if not session:
            await ws.send_json({
                "type": "error",
                "content": f"Session {session_id} not found",
            })
            return

        messages = session.get("messages", [])
        resume_tpl = _TEMPLATES_DIR / "session_resume_prompt.md"
        if resume_tpl.exists():
            context = "\n".join(
                f"[{m['role']}] {m['content']}" for m in messages[-20:]
            )
            prompt = resume_tpl.read_text().replace("{{context}}", context)
            await self._agent.send(prompt)

        # Point session store at this session for continued recording
        self._sessions.start_session(session_id)

        await ws.send_json({
            "type": "session_resumed",
            "session_id": session_id,
            "message_count": len(messages),
        })

    @staticmethod
    def _load_suggestions() -> list[str]:
        path = cfg.data_dir / "suggestions.txt"
        if not path.exists():
            return []
        return [
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
