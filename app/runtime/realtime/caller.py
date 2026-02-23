"""Azure Communication Services caller -- initiates and answers phone calls."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


class AcsCaller:
    """Manages ACS call automation for outbound and inbound calls."""

    def __init__(
        self,
        source_number: str,
        acs_connection_string: str,
        acs_callback_path: str | None = None,
        acs_media_streaming_websocket_path: str | None = None,
        *,
        resolve_urls: Any | None = None,
        resolve_source_number: Any | None = None,
    ) -> None:
        self._static_source_number = source_number
        self.acs_connection_string = acs_connection_string
        self._static_callback = acs_callback_path
        self._static_ws = acs_media_streaming_websocket_path
        self._resolve_urls = resolve_urls
        self._resolve_source_number = resolve_source_number
        self._client: Any = None

    @property
    def source_number(self) -> str:
        if self._resolve_source_number:
            return self._resolve_source_number() or self._static_source_number
        return self._static_source_number

    @property
    def acs_callback_path(self) -> str:
        if self._resolve_urls:
            cb, _ = self._resolve_urls()
            return cb
        return self._static_callback or ""

    @property
    def acs_media_streaming_websocket_path(self) -> str:
        if self._resolve_urls:
            _, ws = self._resolve_urls()
            return ws
        return self._static_ws or ""

    def _ensure_client(self) -> Any:
        if self._client is None:
            from azure.communication.callautomation import CallAutomationClient

            self._client = CallAutomationClient.from_connection_string(self.acs_connection_string)
        return self._client

    @staticmethod
    def _build_media_config(ws_url: str) -> Any:
        """Build the shared ``MediaStreamingOptions`` for ACS calls."""
        from azure.communication.callautomation import (
            AudioFormat,
            MediaStreamingAudioChannelType,
            MediaStreamingContentType,
            MediaStreamingOptions,
            StreamingTransportType,
        )

        return MediaStreamingOptions(
            transport_url=ws_url,
            transport_type=StreamingTransportType.WEBSOCKET,
            content_type=MediaStreamingContentType.AUDIO,
            audio_channel_type=MediaStreamingAudioChannelType.MIXED,
            start_media_streaming=True,
            enable_bidirectional=True,
            audio_format=AudioFormat.PCM24_K_MONO,
        )

    async def initiate_call(self, target_number: str) -> None:
        from azure.communication.callautomation import PhoneNumberIdentifier

        callback_url = self.acs_callback_path
        ws_url = self.acs_media_streaming_websocket_path
        if not callback_url or not callback_url.startswith("https://"):
            raise ValueError(f"ACS requires an HTTPS callback URL. Got: {callback_url!r}")
        if not ws_url or not ws_url.startswith("wss://"):
            raise ValueError(f"ACS requires a WSS media streaming URL. Got: {ws_url!r}")

        client = self._ensure_client()
        target = PhoneNumberIdentifier(target_number)
        source = PhoneNumberIdentifier(self.source_number)
        media_config = self._build_media_config(ws_url)

        logger.info(
            "Initiating outbound call: target=%s, source=%s, callback=%s, ws=%s",
            target_number, self.source_number, callback_url, ws_url,
        )
        try:
            result = client.create_call(
                target, callback_url,
                media_streaming=media_config,
                source_caller_id_number=source,
            )
            logger.info(
                "Outbound call created: target=%s, call_connection_id=%s",
                target_number, getattr(result, "call_connection_id", "unknown"),
            )
        except Exception as exc:
            logger.error("ACS create_call FAILED: target=%s, error=%s", target_number, exc, exc_info=True)
            raise

    async def answer_inbound_call(self, incoming_call_context: str) -> None:
        client = self._ensure_client()
        media_config = self._build_media_config(self.acs_media_streaming_websocket_path)
        logger.info("Answering inbound call")
        client.answer_call(incoming_call_context, self.acs_callback_path, media_streaming=media_config)
        logger.info("Inbound call answered")

    async def outbound_call_handler(self, request: web.Request) -> web.Response:
        from azure.core.messaging import CloudEvent

        body = await request.json()
        for event_dict in body:
            event = CloudEvent.from_dict(event_dict)
            if event.data is None:
                continue
            call_id = event.data.get("callConnectionId", "?")
            logger.info("ACS event %s for call %s", event.type, call_id)

            if event.type == "Microsoft.Communication.CallConnected":
                logger.info("Call connected: %s", call_id)
            elif event.type == "Microsoft.Communication.CreateCallFailed":
                info = event.data.get("resultInformation", {})
                logger.error(
                    "CreateCallFailed: call=%s code=%s subcode=%s message=%s",
                    call_id, info.get("code"), info.get("subCode"), info.get("message"),
                )
            elif event.type == "Microsoft.Communication.CallDisconnected":
                info = event.data.get("resultInformation", {})
                logger.info(
                    "Call disconnected: call=%s code=%s subcode=%s message=%s",
                    call_id, info.get("code"), info.get("subCode"), info.get("message"),
                )

        return web.Response(status=200)

    async def inbound_call_handler(self, request: web.Request) -> web.Response:
        from azure.eventgrid import EventGridEvent

        if request.headers.get("aeg-event-type") == "SubscriptionValidation":
            data = await request.json()
            code = data[0]["data"]["validationCode"]
            return web.json_response({"validationResponse": code})

        try:
            event_data = await request.json()
            for event_dict in event_data:
                event = EventGridEvent.from_dict(event_dict)
                if event.event_type == "Microsoft.Communication.IncomingCall":
                    context = event.data["incomingCallContext"]
                    await self.answer_inbound_call(context)
                    logger.info("Incoming call answered")
                    return web.Response(status=200)
        except Exception as exc:
            logger.error("Error handling inbound call: %s", exc, exc_info=True)
            return web.Response(status=500, text=str(exc))

        return web.Response(status=200)
