"""System prompt and template directory for the Realtime voice model."""

from __future__ import annotations

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

REALTIME_SYSTEM_PROMPT: str = (TEMPLATES_DIR / "realtime_prompt.md").read_text()
