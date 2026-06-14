# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Integration test (Task 11.3): graceful shutdown / request draining.

Exercises :mod:`kiro.shutdown` (``ShutdownState``, ``GracefulShutdownMiddleware``
and ``drain_on_shutdown``) at the raw ASGI layer with a controllable downstream
app, so the draining behaviour can be observed deterministically without binding
a real socket (design.md "测试策略 → 集成测试 → 优雅停机", Requirement 4.5):

- in-flight requests (including streamed SSE responses) complete before the drain
  hook returns, and
- once shutdown has begun, brand-new requests are rejected with ``503``.

Requirements: 4.5
"""

import asyncio

import pytest

from kiro.shutdown import (
    GracefulShutdownMiddleware,
    ShutdownState,
    drain_on_shutdown,
)


# --------------------------------------------------------------------------------------------------
# ASGI test helpers
# --------------------------------------------------------------------------------------------------

def _http_scope(path: str = "/v1/chat/completions") -> dict:
    """A minimal ASGI HTTP scope."""
    return {"type": "http", "method": "POST", "path": path, "headers": []}


async def _noop_receive() -> dict:
    """Receive callable that yields a single empty request body."""
    return {"type": "http.request", "body": b"", "more_body": False}


class _SendCollector:
    """Collects ASGI ``send`` messages so assertions can inspect the response."""

    def __init__(self) -> None:
        self.messages: list = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self):
        for message in self.messages:
            if message.get("type") == "http.response.start":
                return message.get("status")
        return None

    @property
    def body(self) -> bytes:
        chunks = [
            m.get("body", b"")
            for m in self.messages
            if m.get("type") == "http.response.body"
        ]
        return b"".join(chunks)


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses (cooperative)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_waits_for_in_flight_then_rejects_new_requests():
    """
    A request that is in flight when shutdown begins is allowed to finish before
    ``drain_on_shutdown`` returns, while a brand-new request that arrives after
    shutdown has begun is rejected with ``503``.

    Requirements: 4.5
    """
    state = ShutdownState()
    gate = asyncio.Event()  # closed -> the in-flight request is "still working"
    entered = asyncio.Event()

    async def gated_app(scope, receive, send):
        entered.set()
        await gate.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    middleware = GracefulShutdownMiddleware(gated_app, state)

    # Launch the in-flight request; the middleware increments the counter before
    # awaiting the (gated) downstream app.
    inflight_send = _SendCollector()
    inflight = asyncio.create_task(
        middleware(_http_scope(), _noop_receive, inflight_send)
    )
    assert await _wait_until(lambda: state.in_flight == 1)
    assert entered.is_set()

    # Begin draining (grace period generous - it should be released well within).
    drain = asyncio.create_task(drain_on_shutdown(state, timeout=5.0))

    # Draining must NOT complete while the request is still in flight.
    await asyncio.sleep(0.1)
    assert not drain.done(), "drain returned before the in-flight request finished"
    assert state.is_shutting_down is True
    assert state.in_flight == 1

    # A brand-new request that arrives mid-shutdown is rejected with 503 and the
    # downstream app is never entered for it.
    rejected_send = _SendCollector()
    await middleware(_http_scope(), _noop_receive, rejected_send)
    assert rejected_send.status == 503
    assert b"service_unavailable" in rejected_send.body
    assert state.in_flight == 1, "a rejected request must not affect the counter"

    # Release the in-flight request; it completes and draining then succeeds.
    gate.set()
    await asyncio.wait_for(inflight, timeout=2.0)
    drained = await asyncio.wait_for(drain, timeout=2.0)

    assert drained is True
    assert state.in_flight == 0
    assert inflight_send.status == 200
    assert inflight_send.body == b"done"


@pytest.mark.asyncio
async def test_new_request_rejected_with_503_once_shutting_down():
    """
    Once :class:`ShutdownState` is shutting down, the middleware short-circuits
    new requests with a ``503`` JSON body and does not invoke the application.

    Requirements: 4.5
    """
    state = ShutdownState()
    app_called = False

    async def app(scope, receive, send):
        nonlocal app_called
        app_called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = GracefulShutdownMiddleware(app, state)
    state.begin_shutdown()

    send = _SendCollector()
    await middleware(_http_scope(), _noop_receive, send)

    assert app_called is False
    assert send.status == 503
    assert b"shutting down" in send.body
    # The 503 carries a Retry-After header so clients / the load balancer retry.
    start = next(m for m in send.messages if m["type"] == "http.response.start")
    header_names = {name for name, _ in start["headers"]}
    assert b"retry-after" in header_names


@pytest.mark.asyncio
async def test_drain_times_out_when_request_never_completes():
    """
    If an in-flight request does not finish within the grace period,
    ``drain_on_shutdown`` returns ``False`` (the caller then proceeds to exit).

    Requirements: 4.5
    """
    state = ShutdownState()
    gate = asyncio.Event()  # never set -> request hangs

    async def hanging_app(scope, receive, send):
        await gate.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = GracefulShutdownMiddleware(hanging_app, state)
    inflight = asyncio.create_task(middleware(_http_scope(), _noop_receive, _SendCollector()))
    assert await _wait_until(lambda: state.in_flight == 1)

    drained = await drain_on_shutdown(state, timeout=0.2)
    assert drained is False
    assert state.in_flight == 1

    # Clean up the still-hanging request task.
    gate.set()
    await asyncio.wait_for(inflight, timeout=2.0)
    assert state.in_flight == 0


@pytest.mark.asyncio
async def test_streamed_response_drains_before_exit():
    """
    A long-lived streamed (SSE-style) response keeps the in-flight counter raised
    until the *entire* body - including frames sent after shutdown began - has
    been emitted, so draining waits for the stream to finish.

    Requirements: 4.5
    """
    state = ShutdownState()
    mid_stream = asyncio.Event()
    release = asyncio.Event()

    async def streaming_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        # First SSE frame is emitted immediately (first-token behaviour)...
        await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
        mid_stream.set()
        await release.wait()
        # ...then the final frame + terminator only after the gate is released.
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False})

    middleware = GracefulShutdownMiddleware(streaming_app, state)
    send = _SendCollector()
    inflight = asyncio.create_task(middleware(_http_scope(), _noop_receive, send))

    # Stream has produced its first frame but is not finished: still in flight.
    assert await _wait_until(lambda: mid_stream.is_set())
    assert state.in_flight == 1

    # Shutdown begins mid-stream; the drain must wait for the stream to complete.
    drain = asyncio.create_task(drain_on_shutdown(state, timeout=5.0))
    await asyncio.sleep(0.1)
    assert not drain.done()

    release.set()
    await asyncio.wait_for(inflight, timeout=2.0)
    drained = await asyncio.wait_for(drain, timeout=2.0)

    assert drained is True
    assert state.in_flight == 0
    # The complete stream (first frame + terminator) was delivered.
    assert send.body == b"data: 1\n\ndata: [DONE]\n\n"
