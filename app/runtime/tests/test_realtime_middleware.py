"""Tests for realtime middleware conversion functions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.runtime.realtime.middleware import (
    RealtimeMiddleTier,
    _ToolCall,
    _acs_to_openai,
    _openai_to_acs,
)


class TestToolCall:
    def test_fields(self) -> None:
        tc = _ToolCall("call-1", "prev-1")
        assert tc.call_id == "call-1"
        assert tc.previous_id == "prev-1"


class TestAcsToOpenai:
    def test_audio_metadata(self) -> None:
        msg = {"kind": "AudioMetadata"}
        result = _acs_to_openai(msg, tools=[], system_message="sys", voice="shimmer")
        assert result is not None
        assert result["type"] == "session.update"
        assert result["session"]["voice"] == "shimmer"
        assert result["session"]["instructions"] == "sys"

    def test_audio_metadata_includes_tools(self) -> None:
        tools = [{"type": "function", "name": "test_tool"}]
        result = _acs_to_openai(
            {"kind": "AudioMetadata"}, tools=tools, system_message="sys", voice="alloy"
        )
        assert result["session"]["tools"] == tools

    def test_audio_data(self) -> None:
        msg = {"kind": "AudioData", "audioData": {"data": "base64data"}}
        result = _acs_to_openai(msg, tools=[], system_message="sys", voice="alloy")
        assert result is not None
        assert result["type"] == "input_audio_buffer.append"
        assert result["audio"] == "base64data"

    def test_audio_data_no_data(self) -> None:
        msg = {"kind": "AudioData", "audioData": {}}
        result = _acs_to_openai(msg, tools=[], system_message="sys", voice="alloy")
        assert result is None

    def test_unknown_kind(self) -> None:
        msg = {"kind": "SomethingElse"}
        result = _acs_to_openai(msg, tools=[], system_message="sys", voice="alloy")
        assert result is None

    def test_no_kind(self) -> None:
        msg = {"data": "test"}
        result = _acs_to_openai(msg, tools=[], system_message="sys", voice="alloy")
        assert result is None


class TestOpenaiToAcs:
    def test_audio_delta(self) -> None:
        msg = {"type": "response.audio.delta", "delta": "audiodata"}
        result = _openai_to_acs(msg)
        assert result is not None
        assert result["kind"] == "AudioData"
        assert result["audioData"]["data"] == "audiodata"

    def test_speech_started(self) -> None:
        msg = {"type": "input_audio_buffer.speech_started"}
        result = _openai_to_acs(msg)
        assert result is not None
        assert result["kind"] == "StopAudio"

    def test_unknown_type(self) -> None:
        msg = {"type": "session.created"}
        result = _openai_to_acs(msg)
        assert result is None


class TestRealtimeMiddleTier:
    def test_init_with_key(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("test-key"), voice="echo",
        )
        assert mid._key == "test-key"
        assert mid.voice == "echo"
        assert mid._token_provider is None

    def test_set_pending_prompt(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("key"),
        )
        mid.set_pending_prompt("custom prompt", opening_message="Hello caller")
        assert mid._pending_prompt == "custom prompt"
        assert mid._pending_opening_message == "Hello caller"
        assert mid._pending_tools is None
        assert mid._pending_exclusive is False

    def test_set_pending_prompt_exclusive_with_tools(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("key"),
        )
        tools = [{"type": "function", "name": "accept"}]
        mid.set_pending_prompt("verify", tools=tools, exclusive=True)
        assert mid._pending_prompt == "verify"
        assert mid._pending_tools == tools
        assert mid._pending_exclusive is True

    def test_consume_pending_no_pending(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        from app.runtime.realtime.tools import ALL_REALTIME_TOOL_SCHEMAS
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("key"),
        )
        prompt, tools = mid._consume_pending()
        assert prompt == mid.system_message
        assert tools == ALL_REALTIME_TOOL_SCHEMAS

    def test_consume_pending_with_prompt(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        from app.runtime.realtime.tools import ALL_REALTIME_TOOL_SCHEMAS
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("key"),
        )
        mid.set_pending_prompt("test prompt")
        prompt, tools = mid._consume_pending()
        assert mid.system_message in prompt
        assert mid._pending_prompt is None
        assert tools == ALL_REALTIME_TOOL_SCHEMAS

    def test_consume_pending_exclusive_replaces_prompt(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("key"),
        )
        custom_tools = [{"type": "function", "name": "accept"}]
        mid.set_pending_prompt("VERIFY ONLY", tools=custom_tools, exclusive=True)
        prompt, tools = mid._consume_pending()
        # Exclusive prompt replaces the base system message entirely.
        assert prompt == "VERIFY ONLY"
        assert mid.system_message not in prompt
        assert tools == custom_tools
        # Pending state is cleared after consume.
        assert mid._pending_prompt is None
        assert mid._pending_tools is None
        assert mid._pending_exclusive is False

    def test_consume_pending_exclusive_with_opening_message(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("key"),
        )
        custom_tools = [{"type": "function", "name": "accept"}]
        mid.set_pending_prompt(
            "VERIFY ONLY",
            opening_message="Hello, please confirm.",
            tools=custom_tools,
            exclusive=True,
        )
        prompt, tools = mid._consume_pending()
        # Exclusive prompt contains the custom prompt and opening message.
        assert "VERIFY ONLY" in prompt
        assert "Hello, please confirm." in prompt
        # Base system message must NOT be present.
        assert mid.system_message not in prompt
        assert tools == custom_tools

    def test_auth_headers_with_key(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("mykey"),
        )
        headers = mid._auth_headers()
        assert headers == {"api-key": "mykey"}

    def test_auth_headers_no_auth_raises(self) -> None:
        from azure.core.credentials import AzureKeyCredential
        mid = RealtimeMiddleTier(
            "https://endpoint.com", "deploy1",
            AzureKeyCredential("key"),
        )
        mid._key = None
        mid._token_provider = None
        with pytest.raises(ValueError, match="No authentication"):
            mid._auth_headers()
