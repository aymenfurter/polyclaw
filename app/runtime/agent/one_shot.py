"""One-shot Copilot session runner.

Spawns an ephemeral CopilotClient to execute a single prompt. Used by the
scheduler and memory formation to avoid re-using the interactive session.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from copilot import CopilotClient
from copilot.generated.session_events import SessionEventType

from ..config.settings import cfg

logger = logging.getLogger(__name__)


async def auto_approve(input_data: dict, invocation: dict) -> dict:
    return {"permissionDecision": "allow"}


# Type alias for the SDK's pre-tool-use callback signature.
PreToolHook = Callable[[dict, Any], Awaitable[dict]]


async def run_one_shot(
    prompt: str,
    *,
    model: str = "gpt-4.1",
    system_message: str = "",
    timeout: float = 300,
    tools: list[Any] | None = None,
    on_pre_tool_use: PreToolHook | None = None,
) -> str | None:
    opts: dict[str, Any] = {"log_level": "error"}
    if cfg.github_token:
        opts["github_token"] = cfg.github_token

    hook = on_pre_tool_use or auto_approve
    client = CopilotClient(opts)
    await client.start()
    try:
        session_cfg: dict[str, Any] = {
            "model": model,
            "hooks": {"on_pre_tool_use": hook},
        }
        if system_message:
            session_cfg["system_message"] = {"mode": "append", "content": system_message}
        if tools:
            session_cfg["tools"] = tools
        session = await client.create_session(session_cfg)
        return await _send_and_wait(session, prompt, timeout)
    finally:
        await _safe_stop(client)


async def _send_and_wait(session: Any, prompt: str, timeout: float) -> str | None:
    final_text: str | None = None
    done = asyncio.Event()

    def on_event(event: Any) -> None:
        nonlocal final_text
        if event.type == SessionEventType.ASSISTANT_MESSAGE:
            final_text = event.data.content
        elif event.type in (SessionEventType.SESSION_IDLE, SessionEventType.SESSION_ERROR):
            done.set()

    session.on(on_event)
    await session.send({"prompt": prompt})
    await asyncio.wait_for(done.wait(), timeout=timeout)

    try:
        await session.destroy()
    except Exception:
        pass
    return final_text


async def _safe_stop(client: CopilotClient) -> None:
    try:
        await client.stop()
    except Exception:
        logger.debug("Error stopping client", exc_info=True)
