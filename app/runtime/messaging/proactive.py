"""Proactive messaging -- store conversation references, send to all channels."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from botbuilder.core import TurnContext
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ChannelAccount,
    ConversationAccount,
    ConversationReference,
)

from ..config.settings import cfg
from ..media import collect_pending_outgoing
from .cards import drain_pending_cards
from .formatting import strip_markdown

logger = logging.getLogger(__name__)


def _channel_account(data: dict | None) -> ChannelAccount | None:
    return ChannelAccount(**data) if data else None


def _conversation_account(data: dict | None) -> ConversationAccount | None:
    if not data:
        return None
    return ConversationAccount(**{k: v for k, v in data.items() if v is not None})


def _serialize_ref(ref: ConversationReference) -> dict:
    def _acct(a: Any) -> dict | None:
        return {"id": a.id, "name": a.name} if a else None
    return {
        "activity_id": ref.activity_id,
        "user": _acct(ref.user),
        "bot": _acct(ref.bot),
        "conversation": {
            "id": ref.conversation.id, "name": ref.conversation.name,
            "is_group": getattr(ref.conversation, "is_group", None),
        } if ref.conversation else None,
        "channel_id": ref.channel_id,
        "locale": ref.locale,
        "service_url": ref.service_url,
    }


def _deserialize_ref(data: dict) -> ConversationReference:
    return ConversationReference(
        activity_id=data.get("activity_id"),
        user=_channel_account(data.get("user")),
        bot=_channel_account(data.get("bot")),
        conversation=_conversation_account(data.get("conversation")),
        channel_id=data.get("channel_id"),
        locale=data.get("locale"),
        service_url=data.get("service_url"),
    )


class ConversationReferenceStore:
    """JSON-file-backed store for conversation references."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or cfg.conversation_refs_path
        self._refs: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._refs = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load conversation refs: %s", exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._refs, indent=2))

    def upsert(self, ref: ConversationReference) -> None:
        key = f"{ref.channel_id}:{ref.user.id}" if ref.user else ref.channel_id or "unknown"
        self._refs[key] = _serialize_ref(ref)
        self._save()

    def get_all(self) -> list[ConversationReference]:
        return [_deserialize_ref(r) for r in self._refs.values()]

    def remove(self, key: str) -> None:
        if key in self._refs:
            del self._refs[key]
            self._save()

    @property
    def is_empty(self) -> bool:
        return len(self._refs) == 0

    @property
    def count(self) -> int:
        return len(self._refs)


async def send_proactive_message(
    adapter: Any,
    store: ConversationReferenceStore,
    app_id: str,
    message: str,
) -> bool:
    refs = store.get_all()
    if not refs:
        logger.warning("[proactive-send] No conversation references stored -- cannot send proactive message.")
        return False

    logger.debug(
        "[proactive-send] attempting to send to %d ref(s), app_id=%s", len(refs), app_id,
    )
    succeeded = 0
    for ref in refs:
        ref_key = f"{ref.channel_id}:{ref.user.id}" if ref.user else ref.channel_id or "unknown"
        try:
            channel = (ref.channel_id or "").lower()
            send_ok = [True]

            async def _callback(
                turn_context: TurnContext,
                _msg: str = message,
                _ch: str = channel,
                _send_ok: list = send_ok,
                _ref_key: str = ref_key,
            ) -> None:
                all_attachments = (collect_pending_outgoing() or []) + (drain_pending_cards() or [])
                activity = Activity(type=ActivityTypes.message, text=_msg)
                if all_attachments:
                    activity.attachments = all_attachments
                if _ch == "telegram":
                    activity.text = strip_markdown(_msg)
                    activity.text_format = "plain"
                try:
                    await turn_context.send_activity(activity)
                except Exception as send_exc:
                    logger.warning("Proactive send failed for %s: %s", _ref_key, send_exc)
                    _send_ok[0] = False

            effective_bot_id = app_id or (ref.bot.id if ref.bot else None) or ""
            await adapter.continue_conversation(ref, _callback, bot_id=effective_bot_id)
            if send_ok[0]:
                succeeded += 1
        except Exception as exc:
            logger.error("Failed proactive send to %s: %s", ref.channel_id, exc)

    return succeeded > 0
