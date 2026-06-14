# -*- coding: utf-8 -*-

"""
Unit + endpoint-existence tests for Task 4.7.

After refactoring config / account_manager / auth / debug_logger onto the
storage-backend abstraction, the public contract must be unchanged. These tests
assert:

  * Endpoint existence: the FastAPI app still registers ``/v1/models``,
    ``/v1/chat/completions``, ``/v1/messages`` and ``/health`` (Requirements
    10.1, 10.3).
  * Edge cases / boundaries:
      - empty ``credentials.json`` yields no accounts and a ``None`` selection;
      - single-account mode bypasses the circuit breaker (account returned even
        when "broken");
      - ``INVALID_MODEL_ID`` failures do NOT penalize the account (discovery,
        not failure), while a genuine RECOVERABLE error does;
      - token near-expiry detection (``is_token_expiring_soon`` /
        ``is_token_expired``) behaves correctly.

Validates Requirements 10.1, 10.3.

Note: these tests do not mutate ``kiro.config`` / process env, so no config
reload or env restoration is required.
"""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock

from kiro.account_manager import Account, AccountManager
from kiro.account_errors import ErrorType
from kiro.auth import KiroAuthManager
from kiro.config import TOKEN_REFRESH_THRESHOLD


# =============================================================================
# Endpoint existence (Requirements 10.1, 10.3)
# =============================================================================

REQUIRED_ENDPOINTS = {
    "/v1/models",
    "/v1/chat/completions",
    "/v1/messages",
    "/health",
}


@pytest.mark.asyncio
async def test_required_endpoints_are_registered():
    """The refactored app must keep the OpenAI/Anthropic/health endpoints."""
    from main import app

    registered = {route.path for route in app.routes if hasattr(route, "path")}
    missing = REQUIRED_ENDPOINTS - registered
    assert not missing, f"Missing required endpoints: {sorted(missing)}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method",
    [
        ("/health", "GET"),
        ("/v1/models", "GET"),
        ("/v1/chat/completions", "POST"),
        ("/v1/messages", "POST"),
    ],
)
async def test_required_endpoints_expose_expected_methods(path, method):
    """Each preserved endpoint must still expose its expected HTTP method."""
    from main import app

    methods = set()
    for route in app.routes:
        if getattr(route, "path", None) == path:
            methods |= set(getattr(route, "methods", set()) or set())
    assert method in methods, f"{method} not registered for {path} (found {methods})"


# =============================================================================
# Edge case: empty credentials.json
# =============================================================================

@pytest.mark.asyncio
async def test_empty_credentials_yields_no_accounts(tmp_path):
    """An empty credentials.json must load zero accounts and select nothing."""
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("[]")

    manager = AccountManager(
        str(creds_file), str(tmp_path / "state.json"), state_store=AsyncMock()
    )
    await manager.load_credentials()

    assert manager._accounts == {}
    # With no accounts the failover loop returns None (not an exception).
    assert await manager.get_next_account("claude-sonnet-4.5") is None


# =============================================================================
# Edge case: single-account circuit-breaker bypass
# =============================================================================

@pytest.mark.asyncio
async def test_single_account_bypasses_circuit_breaker():
    """
    With exactly one account, the circuit breaker is bypassed: the account is
    returned even with a large failure count and a very recent failure time.
    """
    manager = AccountManager("creds.json", "state.json", state_store=AsyncMock())
    solo = Account(
        id="solo",
        auth_manager=object(),       # already initialized -> no network
        failures=100,                # would be deep in cooldown for multi-account
        last_failure_time=9.0e18,    # "failed" far in the future
        models_cached_at=0.0,
    )
    manager._accounts = {"solo": solo}

    selected = await manager.get_next_account("claude-opus-4.5")
    assert selected is solo


# =============================================================================
# Edge case: INVALID_MODEL_ID does not penalize the account
# =============================================================================

@pytest.mark.asyncio
async def test_invalid_model_id_does_not_penalize_account():
    """INVALID_MODEL_ID is model discovery, not an account failure."""
    state_store = AsyncMock()
    manager = AccountManager("creds.json", "state.json", state_store=state_store)
    account = Account(id="acc1", auth_manager=object(), failures=0)
    manager._accounts = {"acc1": account}

    await manager.report_failure(
        "acc1", "some-model", ErrorType.RECOVERABLE, 400, "INVALID_MODEL_ID"
    )

    # Failure counters untouched ...
    assert account.failures == 0
    assert account.last_failure_time == 0.0
    assert account.stats.failed_requests == 0
    # ... only a (successful-path) total request is counted.
    assert account.stats.total_requests == 1
    state_store.increment_failure.assert_not_called()
    state_store.incr_stats.assert_awaited()


@pytest.mark.asyncio
async def test_recoverable_error_penalizes_account():
    """Contrast case: a genuine RECOVERABLE error DOES increment failures."""
    state_store = AsyncMock()
    manager = AccountManager("creds.json", "state.json", state_store=state_store)
    account = Account(id="acc1", auth_manager=object(), failures=0)
    manager._accounts = {"acc1": account}

    await manager.report_failure(
        "acc1", "some-model", ErrorType.RECOVERABLE, 500, "InternalServerError"
    )

    assert account.failures == 1
    assert account.last_failure_time > 0.0
    assert account.stats.failed_requests == 1
    assert account.stats.total_requests == 1
    state_store.increment_failure.assert_awaited()


# =============================================================================
# Edge case: token near-expiry detection
# =============================================================================

def _make_auth_manager() -> KiroAuthManager:
    return KiroAuthManager(
        refresh_token="test_refresh_token",
        profile_arn="arn:aws:codewhisperer:us-east-1:123456789:profile/test",
        region="us-east-1",
    )


@pytest.mark.asyncio
async def test_token_expiring_soon_true_within_threshold():
    """A token expiring within TOKEN_REFRESH_THRESHOLD is 'expiring soon'."""
    manager = _make_auth_manager()
    manager._expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=TOKEN_REFRESH_THRESHOLD - 30
    )
    assert manager.is_token_expiring_soon() is True


@pytest.mark.asyncio
async def test_token_expiring_soon_false_when_far_in_future():
    """A token well beyond the threshold is not 'expiring soon'."""
    manager = _make_auth_manager()
    manager._expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=TOKEN_REFRESH_THRESHOLD + 3600
    )
    assert manager.is_token_expiring_soon() is False


@pytest.mark.asyncio
async def test_token_expiring_soon_true_when_no_expiry_info():
    """Missing expiry info conservatively reports 'expiring soon'."""
    manager = _make_auth_manager()
    manager._expires_at = None
    assert manager.is_token_expiring_soon() is True


@pytest.mark.asyncio
async def test_token_expired_detection():
    """is_token_expired distinguishes already-expired from still-valid tokens."""
    manager = _make_auth_manager()

    manager._expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert manager.is_token_expired() is True

    manager._expires_at = datetime.now(timezone.utc) + timedelta(seconds=3600)
    assert manager.is_token_expired() is False

    manager._expires_at = None
    assert manager.is_token_expired() is True
