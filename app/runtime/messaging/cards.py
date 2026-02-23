"""Rich card support -- Adaptive Cards, Hero Cards, and carousels.

Cards are queued in-memory and drained by the bot / proactive messaging
layer when the response is delivered. Thread-safe via internal lock.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from botbuilder.schema import (
    ActionTypes,
    Attachment,
    CardAction,
    CardImage,
    HeroCard,
    ThumbnailCard,
)

from ..util.singletons import register_singleton

logger = logging.getLogger(__name__)

ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"


class CardQueue:
    """Thread-safe queue for pending card attachments."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cards: list[Attachment] = []

    def enqueue(self, attachment: Attachment) -> None:
        with self._lock:
            self._cards.append(attachment)

    def drain(self) -> list[Attachment]:
        with self._lock:
            cards = list(self._cards)
            self._cards.clear()
        return cards


_default_queue = CardQueue()


def drain_pending_cards() -> list[Attachment]:
    return _default_queue.drain()


def _reset_default_queue() -> None:
    """Drain the global card queue (for test isolation)."""
    _default_queue.drain()


register_singleton(_reset_default_queue)


# -- attachment builders ---------------------------------------------------


def _adaptive_card_attachment(card_json: dict) -> Attachment:
    card_json.setdefault("type", "AdaptiveCard")
    card_json.setdefault("version", "1.5")
    card_json.setdefault("$schema", "http://adaptivecards.io/schemas/adaptive-card.json")
    return Attachment(content_type=ADAPTIVE_CARD_CONTENT_TYPE, content=card_json)


def _build_card_actions(buttons: list[dict] | None) -> list[CardAction] | None:
    if not buttons:
        return None
    actions = [
        CardAction(
            type=ActionTypes.open_url if b.get("type", "openUrl") == "openUrl" else b.get("type", "openUrl"),
            title=b.get("title", ""),
            value=b.get("value", ""),
        )
        for b in buttons
    ]
    return actions or None


def _simple_card_attachment(
    card_class: type,
    content_type: str,
    title: str = "",
    subtitle: str = "",
    text: str = "",
    image_url: str | None = None,
    buttons: list[dict] | None = None,
) -> Attachment:
    images = [CardImage(url=image_url)] if image_url else None
    card = card_class(
        title=title or None,
        subtitle=subtitle or None,
        text=text or None,
        images=images,
        buttons=_build_card_actions(buttons),
    )
    return Attachment(content_type=content_type, content=card)


def _hero_card_attachment(
    title: str = "",
    subtitle: str = "",
    text: str = "",
    image_url: str | None = None,
    buttons: list[dict] | None = None,
) -> Attachment:
    return _simple_card_attachment(HeroCard, "application/vnd.microsoft.card.hero", title, subtitle, text, image_url, buttons)


def _thumbnail_card_attachment(
    title: str = "",
    subtitle: str = "",
    text: str = "",
    image_url: str | None = None,
    buttons: list[dict] | None = None,
) -> Attachment:
    return _simple_card_attachment(ThumbnailCard, "application/vnd.microsoft.card.thumbnail", title, subtitle, text, image_url, buttons)


# -- serialization ---------------------------------------------------------


def attachment_to_dict(att: Attachment) -> dict:
    content = att.content
    if hasattr(content, "__dict__") and not isinstance(content, dict):
        content = _serialize_model(content)
    return {"contentType": att.content_type, "content": content}


def _serialize_model(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, str | int | float | bool):
        return obj
    if isinstance(obj, list):
        return [_serialize_model(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialize_model(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        result = {}
        for key, value in obj.__dict__.items():
            if key.startswith("_") or value is None or key == "additional_properties":
                continue
            result[_to_camel(key)] = _serialize_model(value)
        return result
    return str(obj)


def _to_camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])
