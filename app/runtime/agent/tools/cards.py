"""Card tool definitions for the Copilot agent.

Wraps the card queue and attachment builders from the messaging layer
into ``@define_tool`` functions that the LLM can invoke.
"""

from __future__ import annotations

import json

from copilot import define_tool
from pydantic import BaseModel, Field

from ...messaging.cards import (
    _adaptive_card_attachment,
    _default_queue,
    _hero_card_attachment,
    _thumbnail_card_attachment,
)


# -- parameter models ------------------------------------------------------


class AdaptiveCardParams(BaseModel):
    card_json: str = Field(description="The Adaptive Card payload as a JSON string.")
    fallback_text: str = Field(default="", description="Plain-text fallback for unsupported clients.")


class HeroCardParams(BaseModel):
    title: str = Field(default="", description="Card title")
    subtitle: str = Field(default="", description="Card subtitle")
    text: str = Field(default="", description="Card body text")
    image_url: str | None = Field(default=None, description="URL of the card image")
    buttons: str = Field(default="[]", description="JSON array of button objects.")


class ThumbnailCardParams(BaseModel):
    title: str = Field(default="", description="Card title")
    subtitle: str = Field(default="", description="Card subtitle")
    text: str = Field(default="", description="Card body text")
    image_url: str | None = Field(default=None, description="URL of the thumbnail image")
    buttons: str = Field(default="[]", description="JSON array of button objects.")


class CardCarouselParams(BaseModel):
    cards_json: str = Field(description="JSON array of card objects.")


# -- tool definitions ------------------------------------------------------


@define_tool(description="Send an Adaptive Card to the user with rich layout support.")
def send_adaptive_card(params: AdaptiveCardParams) -> dict:
    try:
        card_data = json.loads(params.card_json)
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid JSON: {exc}"}
    if not isinstance(card_data, dict):
        return {"error": "card_json must be a JSON object."}
    _default_queue.enqueue(_adaptive_card_attachment(card_data))
    return {"status": "queued", "fallback_text": params.fallback_text, "elements": len(card_data.get("body", []))}


@define_tool(description="Send a Hero Card with large image, title, and action buttons.")
def send_hero_card(params: HeroCardParams) -> dict:
    try:
        buttons = json.loads(params.buttons) if params.buttons else []
    except json.JSONDecodeError:
        buttons = []
    _default_queue.enqueue(_hero_card_attachment(title=params.title, subtitle=params.subtitle, text=params.text, image_url=params.image_url, buttons=buttons))
    return {"status": "queued", "title": params.title}


@define_tool(description="Send a Thumbnail Card with smaller image and compact layout.")
def send_thumbnail_card(params: ThumbnailCardParams) -> dict:
    try:
        buttons = json.loads(params.buttons) if params.buttons else []
    except json.JSONDecodeError:
        buttons = []
    _default_queue.enqueue(_thumbnail_card_attachment(title=params.title, subtitle=params.subtitle, text=params.text, image_url=params.image_url, buttons=buttons))
    return {"status": "queued", "title": params.title}


@define_tool(description="Send multiple cards as a horizontal carousel.")
def send_card_carousel(params: CardCarouselParams) -> dict:
    try:
        cards = json.loads(params.cards_json)
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid JSON: {exc}"}
    if not isinstance(cards, list):
        return {"error": "cards_json must be a JSON array."}

    count = 0
    for card in cards:
        card_type = card.pop("type", "hero")
        if card_type == "adaptive":
            _default_queue.enqueue(_adaptive_card_attachment(card))
        elif card_type == "thumbnail":
            buttons = card.get("buttons", [])
            if isinstance(buttons, str):
                buttons = json.loads(buttons)
            _default_queue.enqueue(_thumbnail_card_attachment(title=card.get("title", ""), subtitle=card.get("subtitle", ""), text=card.get("text", ""), image_url=card.get("image_url"), buttons=buttons))
        else:
            buttons = card.get("buttons", [])
            if isinstance(buttons, str):
                buttons = json.loads(buttons)
            _default_queue.enqueue(_hero_card_attachment(title=card.get("title", ""), subtitle=card.get("subtitle", ""), text=card.get("text", ""), image_url=card.get("image_url"), buttons=buttons))
        count += 1

    return {"status": "queued", "card_count": count}


CARD_TOOLS = [send_adaptive_card, send_hero_card, send_thumbnail_card, send_card_carousel]
