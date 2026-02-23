"""Realtime middle-tier -- bridges ACS / browser WebSocket to OpenAI Realtime API."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from aiohttp import ClientWebSocketResponse, web
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

from .prompt import REALTIME_SYSTEM_PROMPT, TEMPLATES_DIR
from .tools import (
    ALL_REALTIME_TOOL_SCHEMAS,
    handle_check_agent_task,
    handle_invoke_agent,
    handle_invoke_agent_async,
)

logger = logging.getLogger(__name__)


class RealtimeMiddleTier:
    """Proxies WebSocket traffic between a client and the OpenAI Realtime API."""

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        credential: AzureKeyCredential | DefaultAzureCredential,
        agent: Any = None,
        voice: str = "alloy",
    ) -> None:
        self.endpoint = endpoint
        self.deployment = deployment
        self.agent = agent
        self.voice = voice
        self.system_message = REALTIME_SYSTEM_PROMPT

        self._key: str | None = None
        self._token_provider = None

        if isinstance(credential, AzureKeyCredential):
            self._key = credential.key
        else:
            self._token_provider = get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default",
            )
            self._token_provider()

        self._tools_pending: dict[str, _ToolCall] = {}
        self._pending_prompt: str | None = None
        self._pending_opening_message: str | None = None
        self._pending_tools: list[dict[str, Any]] | None = None
        self._pending_exclusive: bool = False

    def set_pending_prompt(
        self,
        prompt: str | None,
        *,
        opening_message: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        exclusive: bool = False,
    ) -> None:
        """Stage a prompt override for the next Realtime session.

        Parameters
        ----------
        prompt:
            Custom prompt text.  When *exclusive* is ``False`` (default)
            this is appended to the base system prompt via the call-
            instructions template.  When *exclusive* is ``True`` the
            prompt **replaces** the base system prompt entirely.
        opening_message:
            Optional opening greeting for the voice agent.
        tools:
            When provided, these tool schemas replace the default
            ``ALL_REALTIME_TOOL_SCHEMAS`` for the session.
        exclusive:
            If ``True``, *prompt* is the entire system message and
            *tools* are the only tools available.
        """
        self._pending_prompt = prompt
        self._pending_opening_message = opening_message
        self._pending_tools = tools
        self._pending_exclusive = exclusive

    def _consume_pending(self) -> tuple[str, list[dict[str, Any]]]:
        """Consume staged overrides, returning *(prompt, tools)*."""
        base = self.system_message
        prompt = self._pending_prompt
        opening_message = self._pending_opening_message
        tools_override = self._pending_tools
        exclusive = self._pending_exclusive

        # Reset pending state.
        self._pending_prompt = None
        self._pending_opening_message = None
        self._pending_tools = None
        self._pending_exclusive = False

        # Build effective prompt.
        if exclusive and prompt:
            # In exclusive mode the custom prompt replaces the base
            # system message entirely.  If an opening message was
            # provided, append a short instruction so the Realtime
            # model speaks first.
            if opening_message:
                effective_prompt = (
                    prompt
                    + "\n\nWhen the call connects, your FIRST spoken "
                    "message MUST be:\n"
                    f'"{opening_message}"\n'
                    "After delivering this opening message, wait for "
                    "the user to respond."
                )
            else:
                effective_prompt = prompt
        else:
            parts: list[str] = [base]
            if prompt:
                template = (TEMPLATES_DIR / "realtime_call_instructions.md").read_text()
                parts.append(template.format(prompt=prompt))
            if opening_message:
                template = (TEMPLATES_DIR / "realtime_opening_message.md").read_text()
                parts.append(template.format(opening_message=opening_message))
            effective_prompt = "\n\n".join(parts) if len(parts) > 1 else base

        effective_tools = tools_override if tools_override is not None else ALL_REALTIME_TOOL_SCHEMAS
        return effective_prompt, effective_tools

    async def forward_messages(self, client_ws: web.WebSocketResponse, is_acs: bool) -> None:
        effective_prompt, effective_tools = self._consume_pending()
        tool_names = [t.get("name", "?") for t in effective_tools]
        logger.info(
            "Realtime session starting (is_acs=%s, custom_prompt=%s, tools=%s)",
            is_acs, effective_prompt != self.system_message, tool_names,
        )

        async with aiohttp.ClientSession(base_url=self.endpoint) as session:
            params = {"api-version": "2025-04-01-preview", "deployment": self.deployment}
            headers = self._auth_headers()

            async with session.ws_connect("/openai/realtime", headers=headers, params=params) as server_ws:

                async def client_to_server() -> None:
                    async for msg in client_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            await self._process_to_server(
                                data, client_ws, server_ws, is_acs,
                                effective_prompt=effective_prompt,
                                effective_tools=effective_tools,
                            )

                async def server_to_client() -> None:
                    async for msg in server_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            await self._process_to_client(data, client_ws, server_ws, is_acs)

                try:
                    await asyncio.gather(client_to_server(), server_to_client())
                except ConnectionResetError:
                    pass

    async def _process_to_server(
        self,
        data: Any,
        client_ws: web.WebSocketResponse,
        server_ws: ClientWebSocketResponse,
        is_acs: bool,
        *,
        effective_prompt: str | None = None,
        effective_tools: list[dict[str, Any]] | None = None,
    ) -> None:
        prompt = effective_prompt or self.system_message
        tools = effective_tools if effective_tools is not None else ALL_REALTIME_TOOL_SCHEMAS
        if is_acs:
            data = _acs_to_openai(data, tools=tools, system_message=prompt, voice=self.voice)
        if data is None:
            return

        if data.get("type") == "session.update":
            session = data.setdefault("session", {})
            session["voice"] = self.voice
            session["instructions"] = prompt
            session["tool_choice"] = "auto"
            session["tools"] = tools
            tool_names = [t.get("name", "?") for t in tools]
            logger.info(
                "[middleware] session.update prepared: tools=%s prompt_len=%d",
                tool_names, len(prompt),
            )

        await server_ws.send_str(json.dumps(data))

    async def _process_to_client(
        self,
        message: Any,
        client_ws: web.WebSocketResponse,
        server_ws: ClientWebSocketResponse,
        is_acs: bool,
    ) -> None:
        if message is None:
            return

        msg_type = message.get("type", "")

        if msg_type == "session.created":
            session = message.get("session", {})
            session["instructions"] = ""
            session["tools"] = []
            session["tool_choice"] = "none"
            session["max_response_output_tokens"] = None

        elif msg_type == "session.updated":
            logger.info("[middleware] session.updated received, sending response.create")
            await server_ws.send_json({"type": "response.create"})

        elif msg_type == "response.output_item.added":
            if message.get("item", {}).get("type") == "function_call":
                message = None

        elif msg_type == "conversation.item.created":
            item = message.get("item", {})
            if item.get("type") == "function_call":
                call_id = item.get("call_id", "")
                prev_id = message.get("previous_item_id", "")
                if call_id not in self._tools_pending:
                    self._tools_pending[call_id] = _ToolCall(call_id, prev_id)
                message = None
            elif item.get("type") == "function_call_output":
                message = None

        elif msg_type in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            message = None

        elif msg_type == "response.output_item.done":
            item = message.get("item", {})
            if item.get("type") == "function_call":
                await self._execute_tool(item, server_ws)
                message = None

        elif msg_type == "response.done":
            if self._tools_pending:
                logger.info(
                    "[middleware] response.done with %d pending tools, "
                    "sending response.create",
                    len(self._tools_pending),
                )
                self._tools_pending.clear()
                await server_ws.send_json({"type": "response.create"})
            resp = message.get("response", {})
            outputs = resp.get("output", [])
            if any(o.get("type") == "function_call" for o in outputs):
                resp["output"] = [o for o in outputs if o.get("type") != "function_call"]

        if is_acs and message is not None:
            message = _openai_to_acs(message)

        if message is not None:
            await client_ws.send_str(json.dumps(message))

    async def _execute_tool(self, item: dict[str, Any], server_ws: ClientWebSocketResponse) -> None:
        name = item.get("name", "")
        call_id = item.get("call_id", "")
        args_str = item.get("arguments", "{}")

        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {}

        logger.info("Realtime tool call: %s(%s)", name, args_str[:200])

        handler = _TOOL_DISPATCH.get(name)
        if handler:
            result = await handler(args, self.agent)
        else:
            result = f"Unknown tool: {name}"

        await server_ws.send_json({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": result},
        })

    def _auth_headers(self) -> dict[str, str]:
        if self._key:
            return {"api-key": self._key}
        if self._token_provider:
            return {"Authorization": f"Bearer {self._token_provider()}"}
        raise ValueError("No authentication configured for OpenAI Realtime")


# -- tool dispatch table ---------------------------------------------------


async def _dispatch_check_agent_task(args: dict[str, Any], agent: Any) -> str:
    """Thin adapter so check_agent_task matches the ``(args, agent)`` signature."""
    return await handle_check_agent_task(args)


_TOOL_DISPATCH: dict[str, Any] = {
    "invoke_agent": handle_invoke_agent,
    "invoke_agent_async": handle_invoke_agent_async,
    "check_agent_task": _dispatch_check_agent_task,
}


class _ToolCall:
    __slots__ = ("call_id", "previous_id")

    def __init__(self, call_id: str, previous_id: str) -> None:
        self.call_id = call_id
        self.previous_id = previous_id


def _acs_to_openai(
    msg: dict[str, Any],
    *,
    tools: list[dict],
    system_message: str,
    voice: str,
) -> dict[str, Any] | None:
    kind = msg.get("kind")
    if kind == "AudioMetadata":
        return {
            "type": "session.update",
            "session": {
                "voice": voice,
                "tool_choice": "auto",
                "tools": tools,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.7,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                },
                "instructions": system_message,
            },
        }
    if kind == "AudioData":
        audio = msg.get("audioData", {}).get("data")
        if audio:
            return {"type": "input_audio_buffer.append", "audio": audio}
    return None


def _openai_to_acs(msg: dict[str, Any]) -> dict[str, Any] | None:
    msg_type = msg.get("type")
    if msg_type == "response.audio.delta":
        return {"kind": "AudioData", "audioData": {"data": msg.get("delta", "")}}
    if msg_type == "input_audio_buffer.speech_started":
        return {"kind": "StopAudio", "audioData": None, "stopAudio": {}}
    return None
