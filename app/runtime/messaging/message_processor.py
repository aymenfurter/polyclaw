"""Background message processing pipeline."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from botbuilder.core import BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ConversationReference

from ..agent.agent import Agent
from ..config.settings import cfg
from ..media import (
    collect_pending_outgoing,
    extract_outgoing_attachments,
    move_attachments_to_error,
    read_error_details,
)
from ..services.otel import agent_span, record_event
from ..state.memory import MemoryFormation
from ..state.session_store import SessionStore
from .cards import drain_pending_cards
from .formatting import strip_markdown

if TYPE_CHECKING:
    from ..agent.hitl import HitlInterceptor

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4000


class MessageProcessor:

    def __init__(
        self,
        agent: Agent,
        adapter: BotFrameworkAdapter | None,
        memory: MemoryFormation,
        session_store: SessionStore,
        hitl: HitlInterceptor | None = None,
    ) -> None:
        self._agent = agent
        self.adapter = adapter
        self._memory = memory
        self.session_store = session_store
        self._hitl = hitl
        self._lock = asyncio.Lock()

    async def process(self, ref: ConversationReference, prompt: str, channel: str) -> None:
        typing_done = asyncio.Event()
        asyncio.create_task(self._typing_loop(ref, typing_done))

        try:
            with agent_span(
                "bot.agent_turn",
                attributes={"bot.channel": channel, "bot.prompt_length": len(prompt)},
            ):
                async with self._lock:
                    if self._hitl:
                        async def bot_reply(text: str) -> None:
                            await self._send_proactive_reply(ref, text, channel)

                        self._hitl.set_bot_reply_fn(bot_reply)
                        self._hitl.set_execution_context("bot_processor")
                        self._hitl.set_model(cfg.copilot_model)
                    try:
                        response = await self._agent.send(prompt)
                    finally:
                        if self._hitl:
                            self._hitl.clear_bot_reply_fn()
            if response:
                self._memory.record("assistant", response)
                self.session_store.record("assistant", response)
            typing_done.set()
            await self._send_proactive_reply(ref, response, channel)
        except Exception as exc:
            typing_done.set()
            record_event("agent_error", {"error": str(exc)})
            logger.error("Background processing error: %s", exc, exc_info=True)
            try:
                await self._send_proactive_reply(ref, "An error occurred while processing your message.", channel)
            except Exception as inner:
                logger.error("Failed to send error reply: %s", inner)

    async def _typing_loop(self, ref: ConversationReference, done: asyncio.Event) -> None:
        if not self.adapter or not cfg.bot_app_id:
            return
        while not done.is_set():
            try:
                async def _send_typing(turn_context: TurnContext) -> None:
                    await turn_context.send_activity(Activity(type=ActivityTypes.typing))
                await self.adapter.continue_conversation(ref, _send_typing, bot_id=cfg.bot_app_id)
            except Exception:
                pass
            try:
                await asyncio.wait_for(done.wait(), timeout=3.0)
                break
            except TimeoutError:
                pass

    async def _send_proactive_reply(
        self, ref: ConversationReference, response: str | None, channel: str,
    ) -> None:
        if not self.adapter:
            logger.error("No adapter available for proactive reply")
            return
        bot_id = cfg.bot_app_id
        if not bot_id:
            logger.error("No BOT_APP_ID configured")
            return

        text = response or "(no response)"
        outgoing = extract_outgoing_attachments(text) if response else []
        pending = collect_pending_outgoing()
        if pending:
            outgoing.extend(pending)
        card_attachments = drain_pending_cards()
        if card_attachments:
            outgoing.extend(card_attachments)

        async def _callback(
            turn_context: TurnContext,
            _text: str = text,
            _channel: str = channel,
            _outgoing: list = outgoing,
        ) -> None:
            if _outgoing:
                try:
                    activity = _channel_activity(_text, _channel, attachments=_outgoing)
                    await turn_context.send_activity(activity)
                except Exception as send_exc:
                    logger.error("Failed to send media attachments: %s", send_exc)
                    move_attachments_to_error(_outgoing, str(send_exc))
                    error_details = read_error_details()
                    error_note = ""
                    if error_details:
                        detail_lines = [f"- {e['filename']}: {e['reason']}" for e in error_details[-5:]]
                        error_note = "\n\nMedia delivery failed:\n" + "\n".join(detail_lines)
                    else:
                        error_note = "\n\n_(Some files could not be delivered.)_"
                    for chunk in split_message(_text + error_note):
                        await turn_context.send_activity(_channel_activity(chunk, _channel))
            else:
                for chunk in split_message(_text):
                    try:
                        await turn_context.send_activity(_channel_activity(chunk, _channel))
                    except Exception:
                        await turn_context.send_activity(_channel_activity_plain(chunk))

        await self.adapter.continue_conversation(ref, _callback, bot_id=bot_id)


def _channel_activity(text: str, channel: str, *, attachments: list | None = None) -> Activity:
    activity = Activity(type=ActivityTypes.message, text=text)
    if attachments:
        activity.attachments = attachments
    if channel == "telegram":
        activity.text = strip_markdown(text)
        activity.text_format = "plain"
    return activity


def _channel_activity_plain(text: str) -> Activity:
    return Activity(type=ActivityTypes.message, text=strip_markdown(text), text_format="plain")


def split_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = text.rfind(" ", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks
