# -*- coding: utf-8 -*-

"""
Unit tests for kiro.request_context.

Covers:
- RequestContextMiddleware: request_id generation, header reuse
  (X-Request-Id / X-Amzn-Trace-Id), response header echo, contextvar isolation.
- Structured stdout logging: JSON lines containing timestamp/level/request_id/
  message, and the human-readable "pretty" format.
- Secret redaction hooked into the logging sink via SecretProvider.redact.
- resolve_log_format precedence (json | pretty | auto).

These validate the behaviour required by Requirements 8.1, 8.7 and 6.5.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from kiro.backends.local.secret_provider import LocalSecretProvider, REDACTION_MASK
from kiro.request_context import (
    AMZN_TRACE_ID_HEADER,
    NO_REQUEST_ID,
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    configure_logging,
    generate_request_id,
    get_request_id,
    reset_request_id,
    resolve_log_format,
    set_log_redactor,
    set_request_id,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def restore_logging():
    """
    Restore a sane global loguru configuration after a test mutates it.

    configure_logging() calls logger.remove() which affects the global logger,
    so tests that reconfigure logging must restore it for the rest of the suite.
    """
    yield
    set_log_redactor(None)
    logger.remove()
    logger.add(lambda _m: None, level="INFO", format="{message}")


@pytest.fixture
def context_app():
    """A minimal FastAPI app wrapped only with RequestContextMiddleware."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/echo")
    async def echo():
        # Return the request_id observed inside the handler via the contextvar.
        return {"request_id": get_request_id()}

    return app


# =============================================================================
# Context variable helpers
# =============================================================================

class TestRequestIdContextVar:
    """Tests for the request_id context variable helpers."""

    def test_default_is_sentinel(self):
        """Outside any request, get_request_id returns the sentinel."""
        assert get_request_id() == NO_REQUEST_ID

    def test_set_and_reset(self):
        """set_request_id binds a value; reset restores the previous value."""
        token = set_request_id("abc123")
        assert get_request_id() == "abc123"
        reset_request_id(token)
        assert get_request_id() == NO_REQUEST_ID

    def test_generate_request_id_is_unique(self):
        """generate_request_id returns distinct non-empty ids."""
        a = generate_request_id()
        b = generate_request_id()
        assert a and b and a != b


# =============================================================================
# Middleware behaviour
# =============================================================================

class TestRequestContextMiddleware:
    """Tests for RequestContextMiddleware request_id assignment and echo."""

    def test_generates_request_id_when_absent(self, context_app):
        """A request without correlation headers gets a generated id, echoed back."""
        with TestClient(context_app) as client:
            resp = client.get("/echo")
        assert resp.status_code == 200
        rid = resp.json()["request_id"]
        assert rid and rid != NO_REQUEST_ID
        # The response echoes the same id.
        assert resp.headers[REQUEST_ID_HEADER] == rid

    def test_reuses_incoming_x_request_id(self, context_app):
        """An incoming X-Request-Id header is reused as the correlation id."""
        with TestClient(context_app) as client:
            resp = client.get("/echo", headers={REQUEST_ID_HEADER: "client-supplied-id"})
        assert resp.json()["request_id"] == "client-supplied-id"
        assert resp.headers[REQUEST_ID_HEADER] == "client-supplied-id"

    def test_reuses_amzn_trace_id_when_no_request_id(self, context_app):
        """X-Amzn-Trace-Id (from ALB) is used when X-Request-Id is absent."""
        trace = "Root=1-5759e988-bd862e3fe1be46a994272793"
        with TestClient(context_app) as client:
            resp = client.get("/echo", headers={AMZN_TRACE_ID_HEADER: trace})
        assert resp.json()["request_id"] == trace
        assert resp.headers[REQUEST_ID_HEADER] == trace

    def test_x_request_id_takes_precedence_over_trace(self, context_app):
        """X-Request-Id is preferred over X-Amzn-Trace-Id when both are present."""
        with TestClient(context_app) as client:
            resp = client.get(
                "/echo",
                headers={
                    REQUEST_ID_HEADER: "explicit-id",
                    AMZN_TRACE_ID_HEADER: "Root=trace",
                },
            )
        assert resp.json()["request_id"] == "explicit-id"

    def test_blank_header_is_ignored(self, context_app):
        """A blank correlation header is ignored and a fresh id is generated."""
        with TestClient(context_app) as client:
            resp = client.get("/echo", headers={REQUEST_ID_HEADER: "   "})
        rid = resp.json()["request_id"]
        assert rid and rid.strip() and rid != NO_REQUEST_ID

    def test_context_reset_after_request(self, context_app):
        """The contextvar is reset to the sentinel after the request completes."""
        with TestClient(context_app) as client:
            client.get("/echo", headers={REQUEST_ID_HEADER: "leak-check"})
        assert get_request_id() == NO_REQUEST_ID

    def test_distinct_requests_get_distinct_ids(self, context_app):
        """Two generated requests receive different correlation ids."""
        with TestClient(context_app) as client:
            r1 = client.get("/echo")
            r2 = client.get("/echo")
        assert r1.json()["request_id"] != r2.json()["request_id"]


# =============================================================================
# Structured logging
# =============================================================================

class TestStructuredLogging:
    """Tests for the structured (JSON) stdout logging configuration."""

    def test_json_line_contains_required_fields(self, capsys, restore_logging):
        """A JSON log line includes timestamp, level, request_id and message."""
        configure_logging(level="DEBUG", log_format="json")
        token = set_request_id("req-123")
        try:
            logger.info("hello structured world")
        finally:
            reset_request_id(token)

        out = capsys.readouterr().out.strip().splitlines()
        assert out, "expected at least one log line on stdout"
        entry = json.loads(out[-1])
        assert entry["level"] == "INFO"
        assert entry["message"] == "hello structured world"
        assert entry["request_id"] == "req-123"
        assert "timestamp" in entry and entry["timestamp"]

    def test_json_line_uses_sentinel_without_request(self, capsys, restore_logging):
        """Logs emitted outside a request carry the sentinel request_id."""
        configure_logging(level="INFO", log_format="json")
        logger.info("startup message")
        out = capsys.readouterr().out.strip().splitlines()
        entry = json.loads(out[-1])
        assert entry["request_id"] == NO_REQUEST_ID

    def test_json_line_includes_exception(self, capsys, restore_logging):
        """Exceptions are serialized into the JSON 'exception' field."""
        configure_logging(level="DEBUG", log_format="json")
        try:
            raise ValueError("boom-detail")
        except ValueError:
            logger.opt(exception=True).error("failed op")
        out = capsys.readouterr().out.strip().splitlines()
        entry = json.loads(out[-1])
        assert entry["message"] == "failed op"
        assert "exception" in entry
        assert "ValueError" in entry["exception"]

    def test_pretty_format_writes_human_line(self, capsys, restore_logging):
        """Pretty format emits a readable single line including the request_id."""
        configure_logging(level="INFO", log_format="pretty")
        token = set_request_id("pretty-id")
        try:
            logger.info("pretty message")
        finally:
            reset_request_id(token)
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "pretty message" in combined
        assert "pretty-id" in combined


# =============================================================================
# Redaction hooked into logging
# =============================================================================

class TestLogRedaction:
    """Tests that secret values are redacted from log output (Requirements 6.5)."""

    def test_secret_is_redacted_in_json_logs(self, capsys, restore_logging):
        """A registered secret never appears in clear text in JSON log output."""
        provider = LocalSecretProvider()
        provider.register_secret("super-secret-token-value")
        configure_logging(level="INFO", log_format="json", secret_provider=provider)

        logger.info("token is super-secret-token-value here")
        out = capsys.readouterr().out
        assert "super-secret-token-value" not in out
        assert REDACTION_MASK in out

    def test_secret_is_redacted_in_pretty_logs(self, capsys, restore_logging):
        """Redaction also applies to the human-readable pretty format."""
        provider = LocalSecretProvider()
        provider.register_secret("another-secret-1234")
        configure_logging(level="INFO", log_format="pretty", secret_provider=provider)

        logger.warning("leaking another-secret-1234 now")
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "another-secret-1234" not in combined
        assert REDACTION_MASK in combined

    def test_set_log_redactor_can_be_wired_after_configure(self, capsys, restore_logging):
        """Redaction can be enabled after the sink is configured (start-up flow)."""
        configure_logging(level="INFO", log_format="json")
        provider = LocalSecretProvider()
        provider.register_secret("late-bound-secret-xyz")
        set_log_redactor(provider)

        logger.info("value late-bound-secret-xyz")
        out = capsys.readouterr().out
        assert "late-bound-secret-xyz" not in out

    def test_redaction_never_breaks_logging_on_bad_redactor(self, capsys, restore_logging):
        """A redactor that raises must not break logging."""
        class BadProvider:
            def redact(self, text):
                raise RuntimeError("redactor failure")

        configure_logging(level="INFO", log_format="json", secret_provider=BadProvider())
        logger.info("still logged despite redactor error")
        out = capsys.readouterr().out
        assert "still logged despite redactor error" in out


# =============================================================================
# Format resolution
# =============================================================================

class TestResolveLogFormat:
    """Tests for resolve_log_format precedence."""

    def test_explicit_json(self):
        assert resolve_log_format("json") == "json"

    def test_explicit_pretty(self):
        assert resolve_log_format("pretty") == "pretty"

    def test_auto_resolves_json_for_aws_backend(self):
        assert resolve_log_format("auto", backend_name="aws") == "json"

    def test_auto_resolves_pretty_for_local_backend(self):
        assert resolve_log_format("auto", backend_name="local") == "pretty"

    def test_auto_uses_env_when_no_backend(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "aws")
        assert resolve_log_format("auto") == "json"
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        assert resolve_log_format("auto") == "pretty"

    def test_none_defaults_to_auto(self, monkeypatch):
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        assert resolve_log_format(None) == "pretty"
