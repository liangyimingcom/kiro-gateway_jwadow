# -*- coding: utf-8 -*-

"""
Property-based test for Task 4.4 / design Property 8 (熔断器冷却判定确定性).

The circuit-breaker cooldown decision lives *inline* inside
``AccountManager.get_next_account`` (``kiro/account_manager.py``) and is
interleaved with a probabilistic-retry coin flip. This test drives the REAL
``get_next_account`` cooldown branch but neutralises the probabilistic component
so the deterministic cooldown decision is observable:

  * ``random.random`` is pinned above ``ACCOUNT_PROBABILISTIC_RETRY_CHANCE`` so a
    "broken" account that is still cooling down is always *skipped* (never
    probabilistically retried).
  * ``time.time`` is pinned to a fixed ``now``.

Two accounts are configured. Account ``A`` (the sticky start account) carries
the failure state under test; account ``B`` is healthy. With the coin flip
forced to "skip":

    in cooldown  => A is skipped, manager returns B
    not cooldown => A is half-open, manager returns A

The expected cooldown verdict is computed with the exact formula from the
design property and asserted against the manager's real selection. Determinism
is checked by issuing the selection twice and requiring identical results.

Domain note: the exponential-backoff cooldown is only defined once an account
has failed at least once (the implementation guards on ``failures > 0`` and the
formula uses ``2^(failures-1)``), so ``failures`` is drawn from >= 1.

Uses Hypothesis with max_examples >= 100.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from hypothesis import example, given, settings, strategies as st

from kiro.account_manager import Account, AccountManager
from kiro.config import ACCOUNT_RECOVERY_TIMEOUT, ACCOUNT_MAX_BACKOFF_MULTIPLIER


def _effective_timeout(failures: int) -> float:
    """Exact cooldown window from design Property 8."""
    backoff_multiplier = min(2 ** (failures - 1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)
    return ACCOUNT_RECOVERY_TIMEOUT * backoff_multiplier


async def _select_twice(failures: int, last_failure_time: float, now: float):
    """
    Run the real AccountManager.get_next_account cooldown branch twice with
    time/randomness pinned, returning both selected accounts.
    """
    manager = AccountManager(
        "credentials.json", "state.json", state_store=AsyncMock()
    )
    # Account A: the failing account under test (sticky start index 0).
    account_a = Account(
        id="A",
        auth_manager=object(),      # non-None -> skip lazy init / network
        failures=failures,
        last_failure_time=last_failure_time,
        models_cached_at=0.0,       # 0 -> skip TTL refresh branch
    )
    # Account B: healthy fallback (never in cooldown).
    account_b = Account(
        id="B",
        auth_manager=object(),
        failures=0,
        last_failure_time=0.0,
        models_cached_at=0.0,
    )
    manager._accounts = {"A": account_a, "B": account_b}
    manager._current_account_index = 0  # start the scan at A

    # Pin time and force the probabilistic-retry coin flip to "skip" (a value
    # strictly greater than the retry chance) so the cooldown verdict is the
    # only thing that decides whether A is used.
    with patch("kiro.account_manager.time.time", return_value=now), patch(
        "kiro.account_manager.random.random", return_value=1.0
    ):
        first = await manager.get_next_account("claude-sonnet-4.5")
        second = await manager.get_next_account("claude-sonnet-4.5")
    return first, second


# Feature: aws-cloud-native-gateway, Property 8: 对任意 (failures, last_failure_time, now)，冷却判定为纯函数 is_in_cooldown = (now - last_failure_time) < ACCOUNT_RECOVERY_TIMEOUT * min(2^(failures-1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)
# Validates Requirements 7.4
@settings(max_examples=250, deadline=None)
@given(
    failures=st.integers(min_value=1, max_value=15),
    last_failure_time=st.floats(
        min_value=0.0, max_value=2_000_000_000.0, allow_nan=False, allow_infinity=False
    ),
    offset=st.floats(
        min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False
    ),
)
@example(failures=1, last_failure_time=1_000_000.0, offset=0.0)    # exact boundary
@example(failures=1, last_failure_time=1_000_000.0, offset=-0.5)   # just inside cooldown
@example(failures=13, last_failure_time=1_000_000.0, offset=-0.5)  # backoff multiplier capped
@example(failures=13, last_failure_time=1_000_000.0, offset=10.0)  # capped + recovered
def test_property_8_cooldown_decision_is_pure_and_deterministic(
    failures, last_failure_time, offset
):
    effective_timeout = _effective_timeout(failures)
    now = last_failure_time + effective_timeout + offset

    # Pure-function cooldown verdict (computed with identical float arithmetic
    # to the implementation, so the boundary `<` is evaluated identically).
    is_in_cooldown = (now - last_failure_time) < effective_timeout
    expected_account_id = "B" if is_in_cooldown else "A"

    first, second = asyncio.run(_select_twice(failures, last_failure_time, now))

    assert first is not None and second is not None
    # Real manager selection must match the formula's verdict ...
    assert first.id == expected_account_id
    # ... and be deterministic for identical inputs.
    assert second.id == expected_account_id
