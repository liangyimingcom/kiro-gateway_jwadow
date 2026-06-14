# -*- coding: utf-8 -*-

"""
Unit and property tests for kiro/shutdown.py (graceful shutdown / request draining).

Covers:
- ShutdownState in-flight counter and idle/drain semantics
- get_graceful_shutdown_timeout() resolution (default / env / invalid / negative)
- GracefulShutdownMiddleware: pass-through, in-flight tracking, 503 rejection on shutdown
- drain_on_shutdown(): flag flip + bounded wait for in-flight requests

These tests validate Requirement 4.5 (graceful shutdown / request draining).
"""

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro.shutdown import (
    DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT,
    GRACEFUL_SHUTDOWN_TIMEOUT_KEY,
    GracefulShutdownMiddleware,
    ShutdownState,
    drain_on_shutdown,
    get_graceful_shutdown_timeout,
)


# =============================================================================
# ShutdownState
# =============================================================================

class TestShutdownState:
    """Tests for the in-flight request tracker."""

    def test_initial_state_is_idle_and_accepting(self):
        state = ShutdownState()
        assert state.in_flight == 0
        assert state.is_shutting_down is False

    def test_increment_decrement_counter(self):
        state = ShutdownState()
        assert state.increment() == 1
        assert state.increment() == 2
        assert state.in_flight == 2
        assert state.decrement() == 1
        assert state.decrement() == 0
        assert state.in_flight == 0

    def test_decrement_never_goes_negative(self):
        state = ShutdownState()
        # Defensive clamp: extra decrements must not produce negatives.
        assert state.decrement() == 0
        assert state.decrement() == 0
        assert state.in_flight == 0

    def test_begin_shutdown_sets_flag(self):
        state = ShutdownState()
        state.begin_shutdown()
        assert state.is_shutting_down is True

    def test_reset_restores_accepting_state(self):
        state = ShutdownState()
        state.increment()
        state.begin_shutdown()
        state.reset()
        assert state.in_flight == 0
        assert state.is_shutting_down is False

    @pytest.mark.asyncio
    async def test_wait_for_drain_returns_immediately_when_idle(self):
        state = ShutdownState()
        assert await state.wait_for_drain(timeout=5) is True

    @pytest.mark.asyncio
    async def test_wait_for_drain_times_out_with_in_flight(self):
        state = ShutdownState()
        state.increment()
        # Request never completes -> should time out and return False.
        assert await state.wait_for_drain(timeout=0.05) is False

    @pytest.mark.asyncio
    async def test_wait_for_drain_unblocks_when_request_finishes(self):
        state = ShutdownState()
        state.increment()

        async def finish_later():
            await asyncio.sleep(0.05)
            state.decrement()

        task = asyncio.create_task(finish_later())
        assert await state.wait_for_drain(timeout=5) is True
        await task

    @pytest.mark.asyncio
    async def test_wait_for_drain_zero_timeout_no_wait(self):
        state = ShutdownState()
        state.increment()
        # timeout=0 should return immediately reflecting current (busy) state.
        assert await state.wait_for_drain(timeout=0) is False


# =============================================================================
# get_graceful_shutdown_timeout
# =============================================================================

class TestGetGracefulShutdownTimeout:
    """Tests for grace-period resolution."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(GRACEFUL_SHUTDOWN_TIMEOUT_KEY, raising=False)
        assert get_graceful_shutdown_timeout() == DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(GRACEFUL_SHUTDOWN_TIMEOUT_KEY, "45")
        assert get_graceful_shutdown_timeout() == 45.0

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(GRACEFUL_SHUTDOWN_TIMEOUT_KEY, "not-a-number")
        assert get_graceful_shutdown_timeout() == DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT

    def test_negative_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(GRACEFUL_SHUTDOWN_TIMEOUT_KEY, "-10")
        assert get_graceful_shutdown_timeout() == DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(GRACEFUL_SHUTDOWN_TIMEOUT_KEY, "   ")
        assert get_graceful_shutdown_timeout() == DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT


# =============================================================================
# GracefulShutdownMiddleware
# =============================================================================

def _make_http_scope(path: str = "/v1/models"):
    return {"type": "http", "method": "GET", "path": path, "headers": []}


async def _empty_receive():  # pragma: no cover - not exercised by the dummy app
    return {"type": "http.request", "body": b"", "more_body": False}


class _SendCollector:
    """Collects ASGI messages sent by the middleware/app."""

    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        for m in self.messages:
            if m["type"] == "http.response.start":
                return m["status"]
        return None

    @property
    def body(self):
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )


class TestGracefulShutdownMiddleware:
    """Tests for the in-flight tracking ASGI middleware."""

    @pytest.mark.asyncio
    async def test_passes_through_and_tracks_in_flight(self):
        state = ShutdownState()
        seen_in_flight = {}

        async def app(scope, receive, send):
            # Counter should be incremented while the downstream app runs.
            seen_in_flight["value"] = state.in_flight
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = GracefulShutdownMiddleware(app, state)
        send = _SendCollector()
        await mw(_make_http_scope(), _empty_receive, send)

        assert seen_in_flight["value"] == 1
        assert send.status == 200
        # Counter is decremented back to zero after the response completes.
        assert state.in_flight == 0

    @pytest.mark.asyncio
    async def test_decrements_even_if_app_raises(self):
        state = ShutdownState()

        async def failing_app(scope, receive, send):
            raise RuntimeError("boom")

        mw = GracefulShutdownMiddleware(failing_app, state)
        with pytest.raises(RuntimeError):
            await mw(_make_http_scope(), _empty_receive, _SendCollector())
        # finally block must still decrement the counter.
        assert state.in_flight == 0

    @pytest.mark.asyncio
    async def test_rejects_new_requests_during_shutdown(self):
        state = ShutdownState()
        state.begin_shutdown()
        called = {"app": False}

        async def app(scope, receive, send):
            called["app"] = True

        mw = GracefulShutdownMiddleware(app, state)
        send = _SendCollector()
        await mw(_make_http_scope(), _empty_receive, send)

        assert called["app"] is False
        assert send.status == 503
        assert b"shutting down" in send.body
        assert state.in_flight == 0

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        state = ShutdownState()
        state.begin_shutdown()  # even while shutting down, lifespan must pass through
        called = {"app": False}

        async def app(scope, receive, send):
            called["app"] = True

        mw = GracefulShutdownMiddleware(app, state)
        await mw({"type": "lifespan"}, _empty_receive, _SendCollector())
        assert called["app"] is True


# =============================================================================
# drain_on_shutdown
# =============================================================================

class TestDrainOnShutdown:
    """Tests for the lifespan shutdown drain hook."""

    @pytest.mark.asyncio
    async def test_returns_true_when_no_in_flight(self):
        state = ShutdownState()
        result = await drain_on_shutdown(state, timeout=1)
        assert result is True
        assert state.is_shutting_down is True

    @pytest.mark.asyncio
    async def test_times_out_when_request_stuck(self):
        state = ShutdownState()
        state.increment()
        result = await drain_on_shutdown(state, timeout=0.05)
        assert result is False
        assert state.is_shutting_down is True

    @pytest.mark.asyncio
    async def test_waits_for_in_flight_to_complete(self):
        state = ShutdownState()
        state.increment()

        async def finish_later():
            await asyncio.sleep(0.05)
            state.decrement()

        task = asyncio.create_task(finish_later())
        result = await drain_on_shutdown(state, timeout=5)
        assert result is True
        await task


# =============================================================================
# Property-based tests
# =============================================================================

class TestShutdownStateProperties:
    """Universal invariants of the in-flight counter."""

    @settings(max_examples=100)
    @given(n=st.integers(min_value=0, max_value=200))
    def test_balanced_increments_decrements_end_idle(self, n):
        """For any N, N increments followed by N decrements returns to idle (0)."""
        state = ShutdownState()
        for _ in range(n):
            state.increment()
        assert state.in_flight == n
        for _ in range(n):
            state.decrement()
        assert state.in_flight == 0

    @settings(max_examples=100)
    @given(
        ops=st.lists(st.sampled_from(["inc", "dec"]), max_size=200),
    )
    def test_counter_never_negative(self, ops):
        """Across any interleaving of inc/dec, the counter stays >= 0."""
        state = ShutdownState()
        for op in ops:
            if op == "inc":
                state.increment()
            else:
                state.decrement()
            assert state.in_flight >= 0
