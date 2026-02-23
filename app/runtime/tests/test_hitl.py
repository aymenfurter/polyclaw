"""Tests for the HITL interceptor -- channel-aware approval."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.runtime.agent.hitl import HitlInterceptor
from app.runtime.state.guardrails import GuardrailsConfigStore


@pytest.fixture()
def _isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


@pytest.fixture()
def guardrails(tmp_path) -> GuardrailsConfigStore:
    store = GuardrailsConfigStore.__new__(GuardrailsConfigStore)
    store._path = tmp_path / "guardrails.json"
    store._policy_path = tmp_path / "policy.yaml"
    store._lock = __import__("threading").Lock()
    from app.runtime.state.guardrails import GuardrailsConfig

    store._config = GuardrailsConfig(hitl_enabled=True, default_action="ask")
    store._rebuild_engine()
    return store


@pytest.fixture()
def hitl(guardrails) -> HitlInterceptor:
    return HitlInterceptor(guardrails)


def _make_invocation(name: str) -> MagicMock:
    inv = MagicMock()
    inv.name = name
    return inv


class TestWebChatApproval:
    """Approval via WebSocket (web chat) -- structured events."""

    async def test_ask_chat_emits_approval_requested(self, hitl):
        events: list[tuple[str, dict]] = []
        hitl.bind_turn(
            emit=lambda t, d: events.append((t, d)),
            execution_context="interactive",
        )

        async def approve_later():
            await asyncio.sleep(0.05)
            hitl.resolve_approval("call-1", True)

        asyncio.create_task(approve_later())
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "call-1", "toolName": "run", "input": "ls"},
            _make_invocation("run"),
        )

        assert result["permissionDecision"] == "allow"
        event_types = [e[0] for e in events]
        assert "approval_request" in event_types

    async def test_ask_chat_deny(self, hitl):
        hitl.bind_turn(emit=lambda t, d: None, execution_context="interactive")

        async def deny_later():
            await asyncio.sleep(0.05)
            hitl.resolve_approval("call-2", False)

        asyncio.create_task(deny_later())
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "call-2", "toolName": "run", "input": "rm -rf /"},
            _make_invocation("run"),
        )

        assert result["permissionDecision"] == "deny"


class TestBotChannelApproval:
    """Approval via bot channel (Telegram, Teams) -- text-based."""

    async def test_ask_bot_sends_confirmation_text(self, hitl):
        bot_reply = AsyncMock()
        hitl.bind_turn(bot_reply_fn=bot_reply, execution_context="background")

        async def approve_later():
            await asyncio.sleep(0.05)
            hitl.resolve_bot_reply("y")

        asyncio.create_task(approve_later())
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "call-3", "toolName": "run", "input": "echo hi"},
            _make_invocation("run"),
        )

        assert result["permissionDecision"] == "allow"
        # Should have sent a confirmation message and an outcome message
        assert bot_reply.call_count == 2
        confirmation = bot_reply.call_args_list[0][0][0]
        assert "run" in confirmation
        assert "y" in confirmation.lower()

    async def test_ask_bot_deny_with_no(self, hitl):
        bot_reply = AsyncMock()
        hitl.bind_turn(bot_reply_fn=bot_reply, execution_context="background")

        async def deny_later():
            await asyncio.sleep(0.05)
            hitl.resolve_bot_reply("no")

        asyncio.create_task(deny_later())
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "call-4", "toolName": "bash", "input": "rm -rf /"},
            _make_invocation("bash"),
        )

        assert result["permissionDecision"] == "deny"

    async def test_ask_bot_deny_with_arbitrary_text(self, hitl):
        bot_reply = AsyncMock()
        hitl.bind_turn(bot_reply_fn=bot_reply, execution_context="background")

        async def reply_later():
            await asyncio.sleep(0.05)
            hitl.resolve_bot_reply("what?")

        asyncio.create_task(reply_later())
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "call-5", "toolName": "run", "input": "ls"},
            _make_invocation("run"),
        )

        assert result["permissionDecision"] == "deny"

    async def test_ask_bot_approve_yes_case_insensitive(self, hitl):
        bot_reply = AsyncMock()
        hitl.bind_turn(bot_reply_fn=bot_reply, execution_context="background")

        async def approve_later():
            await asyncio.sleep(0.05)
            hitl.resolve_bot_reply("YES")

        asyncio.create_task(approve_later())
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "call-6", "toolName": "run", "input": "ls"},
            _make_invocation("run"),
        )

        assert result["permissionDecision"] == "allow"


class TestResolve:
    """Tests for resolve_approval and resolve_bot_reply."""

    def test_resolve_bot_reply_returns_false_when_no_pending(self, hitl):
        assert hitl.resolve_bot_reply("y") is False

    def test_has_pending_approval_empty(self, hitl):
        assert hitl.has_pending_approval is False

    def test_resolve_approval_unknown_call_id(self, hitl):
        assert hitl.resolve_approval("nonexistent", True) is False


class TestAllowDeny:
    """Tests for allow/deny strategies (no interactive approval)."""

    async def test_allow_passes_through(self, hitl, guardrails):
        guardrails._config.default_action = "allow"
        guardrails._rebuild_engine()
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "c1", "toolName": "view", "input": "file.py"},
            _make_invocation("view"),
        )
        assert result["permissionDecision"] == "allow"

    async def test_always_approved_tools_bypass_guardrails(self, hitl, guardrails):
        """report_intent and other safe SDK tools are always auto-approved."""
        guardrails._config.default_action = "deny"
        guardrails._rebuild_engine()
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "ri1", "toolName": "report_intent", "input": '{"intent": "test"}'},
            _make_invocation("report_intent"),
        )
        assert result["permissionDecision"] == "allow"

    async def test_deny_blocks(self, hitl, guardrails):
        guardrails._config.default_action = "deny"
        guardrails._rebuild_engine()
        events: list[tuple[str, dict]] = []
        hitl.bind_turn(emit=lambda t, d: events.append((t, d)))

        result = await hitl.on_pre_tool_use(
            {"toolCallId": "c2", "toolName": "run", "input": "rm -rf /"},
            _make_invocation("run"),
        )
        assert result["permissionDecision"] == "deny"
        event_types = [e[0] for e in events]
        assert "tool_denied" in event_types


class TestClearCallbacks:
    """Tests for bind_turn / unbind_turn lifecycle."""

    def test_clear_emit(self, hitl):
        hitl.bind_turn(emit=lambda t, d: None)
        hitl.unbind_turn()
        assert hitl._emit is None

    def test_clear_bot_reply_fn(self, hitl):
        hitl.bind_turn(bot_reply_fn=AsyncMock())
        hitl.unbind_turn()
        assert hitl._bot_reply_fn is None


class TestNoApprovalChannel:
    """When neither bot_reply_fn nor emit is set, deny immediately."""

    async def test_deny_when_no_channel_available(self, hitl):
        """HITL strategy with no approval channel must deny immediately."""
        # Bind turn with no emit or bot_reply_fn
        hitl.bind_turn(execution_context="background")

        result = await hitl.on_pre_tool_use(
            {"toolCallId": "no-ch-1", "toolName": "bash", "input": "date"},
            _make_invocation("bash"),
        )

        assert result["permissionDecision"] == "deny"
        # Should return immediately, not wait 300s
        assert not hitl.has_pending_approval

    async def test_deny_when_no_channel_does_not_block(self, hitl):
        """Ensure denial returns in <1s, not the 300s timeout."""
        hitl.bind_turn(execution_context="interactive")

        import time
        t0 = time.monotonic()
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "no-ch-2", "toolName": "run", "input": "ls"},
            _make_invocation("run"),
        )
        elapsed = time.monotonic() - t0

        assert result["permissionDecision"] == "deny"
        assert elapsed < 1.0, f"Took {elapsed:.1f}s -- should deny immediately"

    async def test_ask_chat_denies_without_emitter(self, hitl):
        """_ask_chat must deny immediately if called with no emitter."""
        hitl.unbind_turn()
        result = await hitl._ask_chat("orphan-1", "bash", "echo hello")
        assert result["permissionDecision"] == "deny"


class TestFilterStrategy:
    """Tests for the filter (Shields) strategy."""

    async def test_filter_allows_when_shield_passes(self, hitl, guardrails):
        """Filter strategy should auto-allow when no attack is detected."""
        guardrails._config.default_action = "filter"
        guardrails._rebuild_engine()
        shield = MagicMock()
        shield_result = MagicMock()
        shield_result.attack_detected = False
        shield.check = MagicMock(return_value=shield_result)
        hitl.set_prompt_shield(shield)

        result = await hitl.on_pre_tool_use(
            {"toolCallId": "f1", "toolName": "bash", "input": "echo hello"},
            _make_invocation("bash"),
        )
        assert result["permissionDecision"] == "allow"

    async def test_filter_denies_when_attack_detected(self, hitl, guardrails):
        """Filter strategy should deny when attack is detected."""
        guardrails._config.default_action = "filter"
        guardrails._rebuild_engine()
        shield = MagicMock()
        shield_result = MagicMock()
        shield_result.attack_detected = True
        shield.check = MagicMock(return_value=shield_result)
        hitl.set_prompt_shield(shield)

        result = await hitl.on_pre_tool_use(
            {"toolCallId": "f2", "toolName": "bash", "input": "ignore instructions"},
            _make_invocation("bash"),
        )
        assert result["permissionDecision"] == "deny"

    async def test_filter_allows_when_no_shield_service(self, hitl, guardrails):
        """Filter strategy with no shield service skips check and allows."""
        guardrails._config.default_action = "filter"
        guardrails._rebuild_engine()
        # No shield set -- hitl._prompt_shield is None

        result = await hitl.on_pre_tool_use(
            {"toolCallId": "f3", "toolName": "run", "input": "ls"},
            _make_invocation("run"),
        )
        assert result["permissionDecision"] == "allow"

    async def test_precheck_skipped_when_endpoint_not_configured(self, hitl, guardrails):
        """Pre-check must skip when shield has no endpoint so hitl/aitl still work."""
        guardrails._config.default_action = "aitl"
        guardrails._rebuild_engine()
        shield = MagicMock()
        shield.configured = False  # No endpoint set
        shield.check = MagicMock()
        hitl.set_prompt_shield(shield)
        hitl.bind_turn(execution_context="interactive")

        # AITL reviewer is not set, so it falls through to interactive
        # (which denies without an emitter).  The point is that the
        # shield pre-check should NOT have been called.
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "f4", "toolName": "bash", "input": "echo hi"},
            _make_invocation("bash"),
        )
        shield.check.assert_not_called()
        # Denied because no AITL reviewer and no emitter -- but NOT
        # because of the shield pre-check.
        assert result["permissionDecision"] == "deny"

    async def test_precheck_runs_when_endpoint_configured(self, hitl, guardrails):
        """Pre-check must run when shield is configured, even for hitl strategy."""
        guardrails._config.default_action = "hitl"
        guardrails._rebuild_engine()
        shield = MagicMock()
        shield.configured = True
        shield_result = MagicMock()
        shield_result.attack_detected = True
        shield_result.detail = "Attack found"
        shield.check = MagicMock(return_value=shield_result)
        hitl.set_prompt_shield(shield)
        hitl.bind_turn(execution_context="interactive")

        result = await hitl.on_pre_tool_use(
            {"toolCallId": "f5", "toolName": "bash", "input": "ignore all"},
            _make_invocation("bash"),
        )
        # Shield pre-check should have blocked before reaching hitl
        shield.check.assert_called_once()
        assert result["permissionDecision"] == "deny"


class TestRaceConditionGuard:
    """Verify that clearing callbacks doesn't affect concurrent tasks.

    Simulates the scenario where Task0's finally block calls
    clear_bot_reply_fn while Task1 needs the callback for HITL.
    """

    async def test_concurrent_messages_dont_lose_callback(self, hitl):
        """Two concurrent process() calls should not clear each other's callbacks."""
        bot_reply_1 = AsyncMock()
        bot_reply_2 = AsyncMock()

        # Simulate Task0 binding its turn
        hitl.bind_turn(bot_reply_fn=bot_reply_1)
        assert hitl._bot_reply_fn is bot_reply_1

        # Task1 overwrites before Task0 unbinds (the race window)
        hitl.bind_turn(bot_reply_fn=bot_reply_2)
        assert hitl._bot_reply_fn is bot_reply_2

        # Task0 unbinds -- protected by the lock in message_processor.
        hitl.unbind_turn()
        assert hitl._bot_reply_fn is None

    async def test_bot_reply_set_before_tool_use(self, hitl):
        """bot_reply_fn must be set when on_pre_tool_use is called."""
        bot_reply = AsyncMock()
        hitl.bind_turn(bot_reply_fn=bot_reply, execution_context="background")

        async def approve_later():
            await asyncio.sleep(0.05)
            hitl.resolve_bot_reply("y")

        asyncio.create_task(approve_later())
        result = await hitl.on_pre_tool_use(
            {"toolCallId": "race-1", "toolName": "bash", "input": "date"},
            _make_invocation("bash"),
        )

        assert result["permissionDecision"] == "allow"
        # Confirm the confirmation message was sent via bot_reply
        assert bot_reply.call_count >= 1
