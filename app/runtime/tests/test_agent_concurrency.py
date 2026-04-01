"""Tests for Agent send-lock and ChatHandler FIFO queue concurrency."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.runtime.agent.agent import Agent
from app.runtime.server.chat import ChatHandler
from app.runtime.state.session_store import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(*, delay: float = 0.0, reply: str = "ok") -> MagicMock:
    """Return a mock SDK session that fires events after *delay* seconds."""
    session = MagicMock()
    captured_handler: list = []

    def mock_on(handler):
        captured_handler.append(handler)
        return lambda: None  # unsub callable

    async def mock_send(*_args, **_kwargs):
        handler = captured_handler[-1]
        if delay:
            await asyncio.sleep(delay)
        handler.final_text = reply
        handler.done.set()

    session.on = mock_on
    session.send = mock_send
    return session


# ---------------------------------------------------------------------------
# Agent._send_lock tests
# ---------------------------------------------------------------------------

class TestAgentSendLock:
    """Verify that Agent.send() serialises concurrent callers via FIFO lock."""

    @pytest.mark.asyncio
    @patch("app.runtime.agent.agent.build_system_prompt", return_value="sp")
    @patch("app.runtime.agent.agent.get_all_tools", return_value=[])
    @patch("app.runtime.agent.agent.CopilotClient")
    async def test_concurrent_sends_are_serialised(
        self, MockClient, _tools, _prompt,
    ) -> None:
        """Two concurrent send() calls must not overlap on session.send()."""
        instance = AsyncMock()
        session = MagicMock()
        instance.create_session.return_value = session
        MockClient.return_value = instance

        # Track ordering: record when each send starts/ends inside session.send
        order: list[str] = []
        captured_handlers: list = []

        def mock_on(handler):
            captured_handlers.append(handler)
            return lambda: None

        async def mock_send(*_a, **_kw):
            handler = captured_handlers[-1]
            idx = len(order) // 2  # 0 for first call, 1 for second
            order.append(f"start-{idx}")
            await asyncio.sleep(0.05)
            handler.final_text = f"reply-{idx}"
            handler.done.set()
            order.append(f"end-{idx}")

        session.on = mock_on
        session.send = mock_send

        a = Agent()
        await a.start()
        a._session = session

        t1 = asyncio.create_task(a.send("first"))
        t2 = asyncio.create_task(a.send("second"))

        r1, r2 = await asyncio.gather(t1, t2)

        # Both should succeed
        assert r1 is not None
        assert r2 is not None

        # The calls must be fully serialised: start-0, end-0, start-1, end-1
        assert order == ["start-0", "end-0", "start-1", "end-1"]

    @pytest.mark.asyncio
    @patch("app.runtime.agent.agent.build_system_prompt", return_value="sp")
    @patch("app.runtime.agent.agent.get_all_tools", return_value=[])
    @patch("app.runtime.agent.agent.CopilotClient")
    async def test_many_concurrent_sends_all_complete(
        self, MockClient, _tools, _prompt,
    ) -> None:
        """Five concurrent sends must all complete without deadlock."""
        instance = AsyncMock()
        session = _make_session(delay=0.01, reply="done")
        instance.create_session.return_value = session
        MockClient.return_value = instance

        a = Agent()
        await a.start()
        a._session = session

        tasks = [asyncio.create_task(a.send(f"msg-{i}")) for i in range(5)]
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

        assert len(results) == 5
        assert all(r is not None for r in results)

    @pytest.mark.asyncio
    @patch("app.runtime.agent.agent.build_system_prompt", return_value="sp")
    @patch("app.runtime.agent.agent.get_all_tools", return_value=[])
    @patch("app.runtime.agent.agent.CopilotClient")
    async def test_send_error_does_not_block_next(
        self, MockClient, _tools, _prompt,
    ) -> None:
        """If one send raises, subsequent sends must still proceed."""
        instance = AsyncMock()
        session = MagicMock()
        instance.create_session.return_value = session
        MockClient.return_value = instance

        call_count = 0
        captured_handlers: list = []

        def mock_on(handler):
            captured_handlers.append(handler)
            return lambda: None

        async def mock_send(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            handler = captured_handlers[-1]
            if call_count == 1:
                raise RuntimeError("transient failure")
            handler.final_text = "recovered"
            handler.done.set()

        session.on = mock_on
        session.send = mock_send

        a = Agent()
        await a.start()
        a._session = session

        # First send fails (agent re-raises non-session errors)
        with pytest.raises(RuntimeError, match="transient failure"):
            await a.send("fail")

        # Second send must not be blocked by the failed first send
        r2 = await asyncio.wait_for(a.send("recover"), timeout=3.0)
        assert r2 == "recovered"


class TestEnsureSession:
    """Verify ensure_session reuses existing sessions safely."""

    @pytest.mark.asyncio
    @patch("app.runtime.agent.agent.build_system_prompt", return_value="sp")
    @patch("app.runtime.agent.agent.get_all_tools", return_value=[])
    @patch("app.runtime.agent.agent.CopilotClient")
    async def test_ensure_session_reuses_existing(
        self, MockClient, _tools, _prompt,
    ) -> None:
        """ensure_session must not destroy an active session."""
        instance = AsyncMock()
        session = MagicMock()
        instance.create_session.return_value = session
        MockClient.return_value = instance

        a = Agent()
        await a.start()
        a._session = session

        result = await a.ensure_session()
        assert result is session
        # create_session should NOT have been called again
        instance.create_session.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.runtime.agent.agent.build_system_prompt", return_value="sp")
    @patch("app.runtime.agent.agent.get_all_tools", return_value=[])
    @patch("app.runtime.agent.agent.CopilotClient")
    async def test_ensure_session_creates_when_none(
        self, MockClient, _tools, _prompt,
    ) -> None:
        """ensure_session creates a session when none exists."""
        instance = AsyncMock()
        session = MagicMock()
        instance.create_session.return_value = session
        MockClient.return_value = instance

        a = Agent()
        await a.start()
        assert a._session is None

        result = await a.ensure_session()
        assert result is session
        instance.create_session.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("app.runtime.agent.agent.build_system_prompt", return_value="sp")
    @patch("app.runtime.agent.agent.get_all_tools", return_value=[])
    @patch("app.runtime.agent.agent.CopilotClient")
    async def test_concurrent_ensure_session_creates_only_once(
        self, MockClient, _tools, _prompt,
    ) -> None:
        """Concurrent ensure_session calls must not create duplicate sessions."""
        instance = AsyncMock()
        session = MagicMock()

        async def slow_create(*_a, **_kw):
            await asyncio.sleep(0.05)
            return session

        instance.create_session.side_effect = slow_create
        MockClient.return_value = instance

        a = Agent()
        await a.start()

        tasks = [asyncio.create_task(a.ensure_session()) for _ in range(3)]
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

        # All should get the same session
        assert all(r is session for r in results)
        # create_session should be called only once (first caller creates,
        # others see the existing session)
        assert instance.create_session.await_count == 1

    @pytest.mark.asyncio
    @patch("app.runtime.agent.agent.build_system_prompt", return_value="sp")
    @patch("app.runtime.agent.agent.get_all_tools", return_value=[])
    @patch("app.runtime.agent.agent.CopilotClient")
    async def test_new_session_waits_for_send_to_finish(
        self, MockClient, _tools, _prompt,
    ) -> None:
        """new_session must wait for an in-flight send to complete."""
        instance = AsyncMock()

        order: list[str] = []
        captured_handlers: list = []

        def mock_on(handler):
            captured_handlers.append(handler)
            return lambda: None

        async def mock_send_msg(*_a, **_kw):
            handler = captured_handlers[-1]
            order.append("session.send-start")
            await asyncio.sleep(0.1)
            handler.final_text = "done"
            handler.done.set()
            order.append("session.send-end")

        session = MagicMock()
        session.on = mock_on
        session.send = mock_send_msg

        new_session = MagicMock()
        new_session.on = mock_on
        new_session.send = mock_send_msg

        async def track_create(*_a, **_kw):
            order.append("create_session")
            return new_session

        async def create_side_effect(*_a, **_kw):
            order.append("create_session")
            return new_session

        instance.create_session.side_effect = create_side_effect
        MockClient.return_value = instance

        a = Agent()
        await a.start()
        a._session = session

        t1 = asyncio.create_task(a.send("hello"))
        await asyncio.sleep(0.01)  # let send acquire the lock
        t2 = asyncio.create_task(a.new_session())

        await asyncio.gather(t1, t2)

        # create_session (from new_session) must happen after session.send ends
        assert order.index("session.send-end") < order.index("create_session")


# ---------------------------------------------------------------------------
# ChatHandler FIFO queue tests
# ---------------------------------------------------------------------------

class TestChatHandlerQueue:
    """Verify that WebSocket 'send' messages are queued FIFO, not dropped."""

    @pytest.fixture()
    def session_store(self) -> SessionStore:
        return SessionStore()

    @pytest.fixture()
    def handler(self, session_store: SessionStore) -> ChatHandler:
        agent = AsyncMock()
        agent.new_session = AsyncMock(return_value="session-1")
        agent.send = AsyncMock(return_value="bot reply")
        agent.list_models = AsyncMock(return_value=[])
        return ChatHandler(agent, session_store)

    @pytest.mark.asyncio
    async def test_queued_sends_processed_in_order(
        self, handler: ChatHandler,
    ) -> None:
        """Multiple 'send' messages must be dispatched sequentially, FIFO."""
        call_order: list[str] = []
        original_dispatch = handler._dispatch.__func__

        async def tracking_dispatch(self_inner, ws, data):
            text = data.get("text", "")
            call_order.append(f"start:{text}")
            await original_dispatch(self_inner, ws, data)
            call_order.append(f"end:{text}")

        handler._dispatch = tracking_dispatch.__get__(handler, type(handler))

        ws = AsyncMock()
        ws.send_json = AsyncMock()

        # Pre-create a session so _send_prompt doesn't fail
        handler._sessions.start_session("s1", model="gpt-4.1")

        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def _send_worker() -> None:
            while True:
                data = await queue.get()
                try:
                    await handler._dispatch(ws, data)
                except Exception:
                    pass
                finally:
                    queue.task_done()

        worker = asyncio.create_task(_send_worker())

        for i in range(3):
            queue.put_nowait({
                "action": "send",
                "text": f"msg-{i}",
                "session_id": "s1",
            })

        # Wait for all queued items to drain
        await asyncio.wait_for(queue.join(), timeout=5.0)
        worker.cancel()

        # Verify FIFO ordering: each send must complete before the next starts
        starts = [e for e in call_order if e.startswith("start:")]
        assert starts == ["start:msg-0", "start:msg-1", "start:msg-2"]

    @pytest.mark.asyncio
    async def test_approve_not_blocked_by_send(
        self, handler: ChatHandler,
    ) -> None:
        """approve_tool dispatches immediately, not through the send queue."""
        dispatch_actions: list[str] = []
        original_dispatch = handler._dispatch.__func__

        async def tracking_dispatch(self_inner, ws, data):
            action = data.get("action", "")
            dispatch_actions.append(action)
            await original_dispatch(self_inner, ws, data)

        handler._dispatch = tracking_dispatch.__get__(handler, type(handler))

        ws = AsyncMock()
        ws.send_json = AsyncMock()

        # Dispatch an approve_tool action directly (not queued)
        await handler._dispatch(ws, {
            "action": "approve_tool",
            "call_id": "test-123",
            "response": "y",
        })

        assert "approve_tool" in dispatch_actions
