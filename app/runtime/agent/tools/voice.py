"""Voice call tool -- initiate outbound calls to the user."""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from copilot import define_tool
from pydantic import BaseModel, Field

from ...config.settings import cfg

logger = logging.getLogger(__name__)


class MakeCallParams(BaseModel):
    prompt: str | None = Field(
        default=None,
        description="Optional custom prompt / instructions for the voice AI agent.",
    )
    opening_message: str | None = Field(
        default=None,
        description="Optional opening message the AI should speak when the call connects.",
    )


@define_tool(
    description=(
        "Initiate an outbound voice call to the user. ALWAYS call this tool "
        "when the user asks to be called -- the target phone number is managed "
        "internally and you do not need to ask the user for it."
    )
)
def make_voice_call(params: MakeCallParams) -> dict:
    target = cfg.voice_target_number
    if not target:
        return {
            "status": "error",
            "message": (
                "No target phone number configured yet. "
                "Ask the user to run: /phone <number>  (e.g. /phone +14155551234)"
            ),
        }
    url = f"http://127.0.0.1:{cfg.admin_port}/api/voice/call"
    body: dict[str, str] = {"number": target}
    if params.prompt:
        body["prompt"] = params.prompt
    if params.opening_message:
        body["opening_message"] = params.opening_message
    payload = json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.admin_secret:
        headers["Authorization"] = f"Bearer {cfg.admin_secret}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    def _fire() -> None:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                logger.info("Voice call API responded: %s", resp.read().decode()[:200])
        except Exception as exc:
            logger.error("Voice call API request failed: %s", exc)

    threading.Thread(target=_fire, daemon=True).start()
    return {"status": "ok", "message": "Call triggered"}
