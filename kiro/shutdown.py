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
Graceful shutdown / request draining (AWS cloud-native refactor).

This module implements SIGTERM-driven graceful shutdown so that, when an ECS
task is asked to stop during a scale-in event, the Gateway:

1. stops accepting *new* requests, and
2. waits for *in-flight* requests (including long-lived SSE streams) to finish,
   up to a configurable grace period, before exiting.

This pairs with ECS task draining + ALB target-group deregistration delay to
implement safe scale-in (Requirement 4.5 / design.md "错误处理 → 优雅停机").

Mechanisms
----------
* **Server level** — the actual draining on SIGTERM is performed by uvicorn's
  built-in graceful shutdown: on signal it stops accepting new connections and
  waits for ongoing requests to complete within ``timeout_graceful_shutdown``.
  :func:`get_graceful_shutdown_timeout` supplies that value (configurable via the
  ``GRACEFUL_SHUTDOWN_TIMEOUT`` environment variable / config provider) so
  ``main.py`` can pass it straight to ``uvicorn.run(...)``.

* **Application level** — :class:`GracefulShutdownMiddleware` tracks the number
  of in-flight HTTP requests in a :class:`ShutdownState`. Once shutdown has begun
  the middleware rejects brand-new requests with ``503 Service Unavailable`` (so
  clients/load balancers can retry elsewhere) while letting already-running
  requests — including SSE streams — finish. :func:`drain_on_shutdown` is invoked
  from the FastAPI lifespan shutdown hook as a backstop that flips the flag and
  waits for the in-flight counter to reach zero (bounded by the grace period).

The counter is intentionally lightweight (a plain integer guarded by the single
asyncio event loop, plus an :class:`asyncio.Event` for "idle" notification); it
adds no locking overhead to the hot request path and does not alter the API
contract or streaming behaviour.
"""

import asyncio
import os
from typing import Optional

from loguru import logger

try:  # Starlette is always available (FastAPI dependency); fall back gracefully.
    from starlette.types import ASGIApp, Receive, Scope, Send
except Exception:  # pragma: no cover - typing only
    ASGIApp = Receive = Scope = Send = object  # type: ignore


# Default grace period (seconds) used when GRACEFUL_SHUTDOWN_TIMEOUT is unset or
# invalid. 30s comfortably covers typical non-streaming requests and short SSE
# streams while staying within common ECS ``stopTimeout`` defaults.
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT: float = 30.0

# Environment / config key for the configurable grace period.
GRACEFUL_SHUTDOWN_TIMEOUT_KEY: str = "GRACEFUL_SHUTDOWN_TIMEOUT"


def get_graceful_shutdown_timeout() -> float:
    """
    Resolve the graceful-shutdown grace period in seconds.

    Sourced (in order) from the active :class:`ConfigProvider` (so the value can
    come from SSM Parameter Store under the AWS backend), then the process
    environment as a fallback, then :data:`DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT`.

    Invalid or negative values fall back to the default so a misconfiguration can
    never disable draining or hang shutdown forever.
    """
    raw: Optional[str] = None
    try:
        from kiro.config import get_config_provider

        raw = get_config_provider().get(GRACEFUL_SHUTDOWN_TIMEOUT_KEY, None)
    except Exception:
        # Config provider not ready (e.g. very early import) -> read env directly.
        raw = None

    if raw is None:
        raw = os.getenv(GRACEFUL_SHUTDOWN_TIMEOUT_KEY)

    if raw is None or str(raw).strip() == "":
        return DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT

    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"Invalid {GRACEFUL_SHUTDOWN_TIMEOUT_KEY}={raw!r}, "
            f"falling back to {DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT}s"
        )
        return DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT

    if value < 0:
        logger.warning(
            f"Negative {GRACEFUL_SHUTDOWN_TIMEOUT_KEY}={value}, "
            f"falling back to {DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT}s"
        )
        return DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT

    return value


class ShutdownState:
    """
    Tracks in-flight HTTP requests and whether graceful shutdown has begun.

    All mutation happens on the single asyncio event loop, so a plain integer
    counter is safe without locking. An :class:`asyncio.Event` exposes an
    "idle" (no in-flight requests) signal that :meth:`wait_for_drain` awaits.
    """

    def __init__(self) -> None:
        self._in_flight: int = 0
        self._shutting_down: bool = False
        # The event is *set* while idle (no in-flight requests).
        self._idle: asyncio.Event = asyncio.Event()
        self._idle.set()

    @property
    def in_flight(self) -> int:
        """Number of requests currently being processed."""
        return self._in_flight

    @property
    def is_shutting_down(self) -> bool:
        """Whether graceful shutdown has been initiated."""
        return self._shutting_down

    def begin_shutdown(self) -> None:
        """Mark the application as shutting down so new requests are rejected."""
        self._shutting_down = True

    def reset(self) -> None:
        """Reset to a fresh, accepting state (primarily for tests/restarts)."""
        self._shutting_down = False
        self._in_flight = 0
        self._idle.set()

    def increment(self) -> int:
        """Register the start of a request. Returns the new in-flight count."""
        self._in_flight += 1
        self._idle.clear()
        return self._in_flight

    def decrement(self) -> int:
        """Register the completion of a request. Returns the new in-flight count."""
        self._in_flight -= 1
        if self._in_flight <= 0:
            self._in_flight = 0
            self._idle.set()
        return self._in_flight

    async def wait_for_drain(self, timeout: Optional[float]) -> bool:
        """
        Wait until there are no in-flight requests, or ``timeout`` seconds elapse.

        Returns ``True`` if all requests drained in time, ``False`` on timeout.
        A ``timeout`` of ``None`` waits indefinitely; ``0`` returns immediately
        with the current state.
        """
        if self._in_flight <= 0:
            return True
        if timeout is not None and timeout <= 0:
            return self._in_flight <= 0
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


class GracefulShutdownMiddleware:
    """
    Pure-ASGI middleware that counts in-flight requests and rejects new ones
    once :class:`ShutdownState` has begun shutting down.

    Implemented at the raw ASGI layer (rather than Starlette's
    ``BaseHTTPMiddleware``) so the in-flight counter is decremented only after
    the *entire* response body — including streamed SSE frames — has been sent.
    This is what allows long-lived streams to drain cleanly.
    """

    def __init__(self, app: "ASGIApp", state: ShutdownState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        # Only HTTP requests participate in draining. Lifespan/websocket pass through.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Reject brand-new requests while shutting down so the client / load
        # balancer can retry against a healthy instance.
        if self.state.is_shutting_down:
            await self._reject(send)
            return

        self.state.increment()
        try:
            await self.app(scope, receive, send)
        finally:
            self.state.decrement()

    async def _reject(self, send: "Send") -> None:
        """Send a minimal 503 response indicating the instance is draining."""
        body = (
            b'{"error":{"message":"Server is shutting down, please retry",'
            b'"type":"service_unavailable"}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"connection", b"close"),
                    (b"retry-after", b"1"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def drain_on_shutdown(
    state: ShutdownState, timeout: Optional[float] = None
) -> bool:
    """
    Lifespan shutdown hook: stop accepting new requests and wait for in-flight
    ones (including SSE streams) to finish, bounded by the grace period.

    Args:
        state: The shared :class:`ShutdownState` used by the middleware.
        timeout: Grace period in seconds. Defaults to
            :func:`get_graceful_shutdown_timeout` when ``None``.

    Returns:
        ``True`` if all in-flight requests completed within the grace period,
        ``False`` if the timeout elapsed with requests still running.
    """
    if timeout is None:
        timeout = get_graceful_shutdown_timeout()

    state.begin_shutdown()

    pending = state.in_flight
    if pending <= 0:
        logger.info("Graceful shutdown: no in-flight requests to drain")
        return True

    logger.info(
        f"Graceful shutdown: draining {pending} in-flight request(s) "
        f"(grace period {timeout}s)..."
    )
    drained = await state.wait_for_drain(timeout)
    if drained:
        logger.info("Graceful shutdown: all in-flight requests completed")
    else:
        logger.warning(
            f"Graceful shutdown: grace period of {timeout}s elapsed with "
            f"{state.in_flight} request(s) still in-flight; proceeding to exit"
        )
    return drained
