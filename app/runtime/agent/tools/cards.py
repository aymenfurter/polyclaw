"""Card tool definitions for the Copilot agent."""

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


ThumbnailCardParams = HeroCardParams


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


def _parse_buttons(raw: str | list) -> list:
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []
    return raw


@define_tool(description="Send a Hero Card with large image, title, and action buttons.")
def send_hero_card(params: HeroCardParams) -> dict:
    buttons = _parse_buttons(params.buttons)
    _default_queue.enqueue(_hero_card_attachment(title=params.title, subtitle=params.subtitle, text=params.text, image_url=params.image_url, buttons=buttons))
    return {"status": "queued", "title": params.title}


@define_tool(description="Send a Thumbnail Card with smaller image and compact layout.")
def send_thumbnail_card(params: ThumbnailCardParams) -> dict:
    buttons = _parse_buttons(params.buttons)
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

    _CARD_BUILDERS = {
        "adaptive": lambda c: _adaptive_card_attachment(c),
        "hero": lambda c: _hero_card_attachment(title=c.get("title", ""), subtitle=c.get("subtitle", ""), text=c.get("text", ""), image_url=c.get("image_url"), buttons=_parse_buttons(c.get("buttons", []))),
        "thumbnail": lambda c: _thumbnail_card_attachment(title=c.get("title", ""), subtitle=c.get("subtitle", ""), text=c.get("text", ""), image_url=c.get("image_url"), buttons=_parse_buttons(c.get("buttons", []))),
    }

    count = 0
    for card in cards:
        card_type = card.pop("type", "hero")
        builder = _CARD_BUILDERS.get(card_type, _CARD_BUILDERS["hero"])
        _default_queue.enqueue(builder(card))
        count += 1

    return {"status": "queued", "card_count": count}


CARD_TOOLS = [send_adaptive_card, send_hero_card, send_thumbnail_card, send_card_carousel]
