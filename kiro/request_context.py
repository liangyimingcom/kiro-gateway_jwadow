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
Request correlation context and structured stdout logging.

This module provides two cooperating pieces that turn the gateway's logs into
something a centralised log service (CloudWatch) can ingest and correlate
across instances, without changing any existing ``logger.*`` call site:

1. :class:`RequestContextMiddleware` - an inbound middleware that assigns a
   ``request_id`` to every request (reusing an incoming ``X-Request-Id`` or
   ``X-Amzn-Trace-Id`` header when present, otherwise generating a UUID),
   stores it in a :class:`contextvars.ContextVar` so it is available to every
   log line emitted while handling that request, and echoes it back on the
   response via the ``X-Request-Id`` header (Requirements 8.7).

2. :func:`configure_logging` - configures loguru so each log line written to
   stdout is structured (a JSON object containing at minimum ``timestamp``,
   ``level``, ``request_id`` and ``message``) for CloudWatch ingestion
   (Requirements 8.1, 8.7), and routes every line through a
   :class:`~kiro.backends.interfaces.SecretProvider`'s ``redact`` so secret
   values never appear in clear text (Requirements 6.5).

The request id is read from the context variable directly inside the logging
sink, so existing modules keep calling ``loguru.logger`` exactly as before and
automatically gain request correlation and redaction.
"""

from __future__ import annotations

import contextvars
import json
import sys
import uuid
from typing import Callable, Optional

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ==================================================================================================
# Request id context variable
# ==================================================================================================

# Header names inspected (in order) for an inbound correlation id before one is
# generated. ``X-Request-Id`` is the de-facto standard; ``X-Amzn-Trace-Id`` is
# injected by AWS load balancers (ALB) so the gateway id can be tied back to the
# ALB access logs.
REQUEST_ID_HEADER = "X-Request-Id"
AMZN_TRACE_ID_HEADER = "X-Amzn-Trace-Id"

# Value used when no request is in scope (e.g. start-up / shutdown logs).
NO_REQUEST_ID = "-"

# Context variable holding the current request's correlation id. ``ContextVar``
# is task-local under asyncio, so concurrent requests never observe each other's
# id.
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kiro_request_id", default=NO_REQUEST_ID
)


def get_request_id() -> str:
    """Return the correlation id for the current request (``"-"`` if none)."""
    return _request_id_var.get()


def set_request_id(request_id: str) -> contextvars.Token:
    """
    Bind ``request_id`` to the current context.

    Returns:
        A :class:`contextvars.Token` that can be passed to :func:`reset_request_id`
        to restore the previous value.
    """
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    """Restore the request id to the value captured before :func:`set_request_id`."""
    try:
        _request_id_var.reset(token)
    except (ValueError, LookupError):  # pragma: no cover - defensive
        # Token created in a different context; fall back to the sentinel.
        _request_id_var.set(NO_REQUEST_ID)


def generate_request_id() -> str:
    """Generate a fresh random correlation id."""
    return uuid.uuid4().hex


def _extract_incoming_request_id(request: Request) -> Optional[str]:
    """
    Return an inbound correlation id from the request headers, if present.

    Prefers ``X-Request-Id`` and falls back to ``X-Amzn-Trace-Id`` (set by ALB).
    Empty / whitespace-only header values are ignored so a fresh id is generated.
    """
    for header in (REQUEST_ID_HEADER, AMZN_TRACE_ID_HEADER):
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()
    return None


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Inbound middleware that establishes a per-request correlation id.

    For every request it:

    - reuses an incoming ``X-Request-Id`` / ``X-Amzn-Trace-Id`` header when
      present, otherwise generates a UUID;
    - stores the id in a context variable so all log lines emitted while the
      request is handled include it (Requirements 8.7);
    - exposes the id on ``request.state.request_id`` for handlers; and
    - echoes the id back to the client via the ``X-Request-Id`` response header.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = _extract_incoming_request_id(request) or generate_request_id()
        token = set_request_id(request_id)
        # Make the id available to route handlers / dependencies as well.
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        # Echo the correlation id back so clients / load balancers can tie the
        # response to their own logs.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


# ==================================================================================================
# Structured stdout logging with secret redaction
# ==================================================================================================

# Pluggable redactor. Holds the active ``SecretProvider.redact`` callable (or a
# no-op). It is stored in a mutable module global so that the logging sink can be
# configured at import time (before the storage backend exists) and have
# redaction enabled later, once the ``SecretProvider`` is available, via
# :func:`set_log_redactor` - without re-adding the sink.
_Redactor = Callable[[str], str]


def _identity_redactor(text: str) -> str:
    return text


_redactor: _Redactor = _identity_redactor


def set_log_redactor(secret_provider) -> None:
    """
    Enable secret redaction for log output using ``secret_provider``.

    ``secret_provider`` must expose a ``redact(text) -> str`` method
    (the :class:`~kiro.backends.interfaces.SecretProvider` protocol). Passing
    ``None`` disables redaction (restores the identity redactor). Requirements 6.5.
    """
    global _redactor
    if secret_provider is None:
        _redactor = _identity_redactor
        return
    redact = getattr(secret_provider, "redact", None)
    if callable(redact):
        _redactor = redact
    else:  # pragma: no cover - defensive
        _redactor = _identity_redactor


def _redact(text: str) -> str:
    """Apply the active redactor, never raising (logging must not fail)."""
    try:
        return _redactor(text)
    except Exception:  # pragma: no cover - defensive
        return text


def _format_exception(record) -> Optional[str]:
    """Return a formatted traceback string for ``record`` if it carries one."""
    exception = record.get("exception")
    if not exception:
        return None
    try:
        import traceback

        return "".join(
            traceback.format_exception(
                exception.type, exception.value, exception.traceback
            )
        ).rstrip()
    except Exception:  # pragma: no cover - defensive
        return str(exception)


def _structured_sink(message) -> None:
    """
    loguru sink that emits one redacted JSON object per log record to stdout.

    Each line contains at minimum ``timestamp``, ``level``, ``request_id`` and
    ``message`` (Requirements 8.1, 8.7); the whole serialized line is passed
    through the active redactor so no secret appears in clear text
    (Requirements 6.5).
    """
    record = message.record
    request_id = record["extra"].get("request_id") or get_request_id()
    entry = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "request_id": request_id,
        "logger": record["name"],
        "function": record["function"],
        "line": record["line"],
        "message": record["message"],
    }
    exc_text = _format_exception(record)
    if exc_text:
        entry["exception"] = exc_text
    line = json.dumps(entry, ensure_ascii=False, default=str)
    sys.stdout.write(_redact(line) + "\n")


def _pretty_sink(message) -> None:
    """
    Human-readable single-line sink (development form).

    Mirrors the previous console format but includes the ``request_id`` and is
    routed through the redactor so secrets are masked in dev too (Requirements 6.5).
    """
    record = message.record
    request_id = record["extra"].get("request_id") or get_request_id()
    ts = record["time"].strftime("%Y-%m-%d %H:%M:%S")
    location = f"{record['name']}:{record['function']}:{record['line']}"
    line = (
        f"{ts} | {record['level'].name: <8} | {request_id} | "
        f"{location} - {record['message']}"
    )
    out = _redact(line)
    exc_text = _format_exception(record)
    if exc_text:
        out = f"{out}\n{_redact(exc_text)}"
    sys.stderr.write(out + "\n")


def resolve_log_format(log_format: Optional[str], backend_name: Optional[str] = None) -> str:
    """
    Resolve the effective log format from a configured value.

    ``"auto"`` (or ``None``) resolves to ``"json"`` when running on the AWS
    storage backend (so CloudWatch ingests structured logs) and ``"pretty"``
    otherwise. Any explicit ``"json"`` / ``"pretty"`` value is honoured as-is.
    """
    value = (log_format or "auto").strip().lower()
    if value in ("json", "pretty"):
        return value
    # "auto" (or anything unrecognised): decide from the active backend.
    import os

    name = (backend_name or os.getenv("STORAGE_BACKEND", "local") or "local").strip().lower()
    return "json" if name == "aws" else "pretty"


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "auto",
    secret_provider=None,
    backend_name: Optional[str] = None,
) -> str:
    """
    (Re)configure loguru's sinks for the gateway.

    Removes existing sinks and installs a single stdout/stderr sink that:

    - emits structured JSON lines (``json`` format) suitable for CloudWatch
      ingestion, including ``timestamp``, ``level``, ``request_id`` and
      ``message`` (Requirements 8.1, 8.7); or a human-readable line (``pretty``
      format) for development; and
    - redacts secret values from every line when a ``SecretProvider`` is wired
      in (Requirements 6.5).

    The ``request_id`` is read from the per-request context variable populated by
    :class:`RequestContextMiddleware`, so existing ``logger.*`` call sites need
    no changes.

    Args:
        level: Minimum log level (e.g. ``"INFO"``).
        log_format: ``"json"`` | ``"pretty"`` | ``"auto"``.
        secret_provider: Optional secret provider used for redaction. May be
            wired in later via :func:`set_log_redactor`.
        backend_name: Active storage backend name, used to resolve ``"auto"``.

    Returns:
        The resolved format actually applied (``"json"`` or ``"pretty"``).
    """
    if secret_provider is not None:
        set_log_redactor(secret_provider)

    resolved = resolve_log_format(log_format, backend_name)

    logger.remove()
    sink = _structured_sink if resolved == "json" else _pretty_sink
    logger.add(sink, level=level, format="{message}")
    return resolved
