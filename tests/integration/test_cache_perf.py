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
Integration test (Task 11.4): TTL read-cache performance benchmark.

Verifies that :class:`kiro.backends.cache_layer.CachingStateStore` fronting a
remote/billed State_Store keeps the *added* latency of reading shared state low
by serving warm-cache reads from process memory (design.md "测试策略 → 集成测试 →
性能基准", Requirement 11.3):

- a cache **hit** must NOT call the wrapped delegate (so it avoids both the
  per-read latency and the billed call), and
- the added overhead of a warm cache-hit read must be well under the 50ms P95
  budget for cached reads.

Timing budgets are deliberately generous and the *primary* assertions are on the
delegate **call count** (a cache hit performs zero delegate calls), which is
robust and not subject to wall-clock flakiness. The benchmark is run both with a
fast in-memory delegate carrying a simulated remote latency and with a real
moto-backed :class:`~kiro.backends.aws.state_store.DynamoStateStore`.

Requirements: 11.3
"""

import asyncio
import os
import time
from collections import Counter
from typing import List

import pytest

from kiro.backends.cache_layer import CachingStateStore
from kiro.backends.interfaces import AccountState, AccountStatsState

# The 50ms P95 budget for the *added* latency of a cached shared-state read.
P95_BUDGET_SECONDS = 0.050

# Simulated round-trip latency of a remote/billed State_Store call. Chosen well
# above the cache-hit cost so "cache hit << delegate call" is unambiguous.
SIMULATED_DELEGATE_LATENCY = 0.020  # 20ms

WARM_READS = 200


def _copy_state(state: AccountState) -> AccountState:
    return AccountState(
        account_id=state.account_id,
        failures=state.failures,
        last_failure_time=state.last_failure_time,
        models_cached_at=state.models_cached_at,
        stats=AccountStatsState(
            total_requests=state.stats.total_requests,
            successful_requests=state.stats.successful_requests,
            failed_requests=state.stats.failed_requests,
        ),
    )


class _MemoryStore:
    """A minimal in-memory :class:`StateStore` used as a delegate under test."""

    def __init__(self) -> None:
        self._accounts: dict = {}
        self._sticky = 0
        self._models: dict = {}

    async def get_account_state(self, account_id: str) -> AccountState:
        state = self._accounts.get(account_id)
        return _copy_state(state) if state else AccountState(account_id=account_id)

    async def increment_failure(self, account_id: str, failure_time: float) -> AccountState:
        state = self._accounts.setdefault(account_id, AccountState(account_id=account_id))
        state.failures += 1
        state.last_failure_time = failure_time
        return _copy_state(state)

    async def reset_failure(self, account_id: str) -> None:
        state = self._accounts.setdefault(account_id, AccountState(account_id=account_id))
        state.failures = 0

    async def incr_stats(self, account_id: str, *, total: int, ok: int, failed: int) -> None:
        state = self._accounts.setdefault(account_id, AccountState(account_id=account_id))
        state.stats.total_requests += total
        state.stats.successful_requests += ok
        state.stats.failed_requests += failed

    async def get_sticky_index(self) -> int:
        return self._sticky

    async def set_sticky_index(self, index: int) -> None:
        self._sticky = index

    async def get_model_mapping(self, model: str) -> List[str]:
        return list(self._models.get(model, []))

    async def add_model_account(self, model: str, account_id: str) -> None:
        accounts = self._models.setdefault(model, [])
        if account_id not in accounts:
            accounts.append(account_id)


class _CountingProxy:
    """
    Wraps any :class:`StateStore`, counting every protocol call and optionally
    adding a fixed latency to model a remote/billed round-trip. Lets the test
    assert that warm cache hits perform **zero** delegate calls.
    """

    def __init__(self, delegate, *, latency: float = 0.0) -> None:
        self._delegate = delegate
        self._latency = latency
        self.calls: Counter = Counter()

    async def _delay(self) -> None:
        if self._latency > 0:
            await asyncio.sleep(self._latency)

    async def get_account_state(self, account_id: str) -> AccountState:
        self.calls["get_account_state"] += 1
        await self._delay()
        return await self._delegate.get_account_state(account_id)

    async def increment_failure(self, account_id: str, failure_time: float) -> AccountState:
        self.calls["increment_failure"] += 1
        await self._delay()
        return await self._delegate.increment_failure(account_id, failure_time)

    async def reset_failure(self, account_id: str) -> None:
        self.calls["reset_failure"] += 1
        await self._delay()
        await self._delegate.reset_failure(account_id)

    async def incr_stats(self, account_id: str, *, total: int, ok: int, failed: int) -> None:
        self.calls["incr_stats"] += 1
        await self._delay()
        await self._delegate.incr_stats(account_id, total=total, ok=ok, failed=failed)

    async def get_sticky_index(self) -> int:
        self.calls["get_sticky_index"] += 1
        await self._delay()
        return await self._delegate.get_sticky_index()

    async def set_sticky_index(self, index: int) -> None:
        self.calls["set_sticky_index"] += 1
        await self._delay()
        await self._delegate.set_sticky_index(index)

    async def get_model_mapping(self, model: str) -> List[str]:
        self.calls["get_model_mapping"] += 1
        await self._delay()
        return await self._delegate.get_model_mapping(model)

    async def add_model_account(self, model: str, account_id: str) -> None:
        self.calls["add_model_account"] += 1
        await self._delay()
        await self._delegate.add_model_account(model, account_id)


def _percentile(samples: List[float], pct: float) -> float:
    """Nearest-rank percentile of ``samples`` (e.g. ``pct=0.95``)."""
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    idx = max(0, min(len(ordered) - 1, int(round(pct * (len(ordered) - 1)))))
    return ordered[idx]


async def _timed_warm_reads(store, account_id: str, n: int) -> List[float]:
    """Issue ``n`` reads and return the per-read wall-clock latency in seconds."""
    latencies: List[float] = []
    for _ in range(n):
        start = time.perf_counter()
        await store.get_account_state(account_id)
        latencies.append(time.perf_counter() - start)
    return latencies


@pytest.mark.asyncio
async def test_cache_hit_avoids_delegate_and_meets_p95_budget():
    """
    With a warm TTL cache, repeated shared-state reads are served from memory:
    the delegate is called exactly once (the initial miss) and never again, and
    the warm-read P95 latency is well under the 50ms cached-read budget.

    Requirements: 11.3
    """
    delegate = _CountingProxy(_MemoryStore(), latency=SIMULATED_DELEGATE_LATENCY)
    cache = CachingStateStore(delegate, ttl_seconds=5.0)  # warm TTL spanning the test

    # First read is a miss -> exactly one delegate call populates the cache.
    first = await cache.get_account_state("acct")
    assert first.account_id == "acct"
    assert delegate.calls["get_account_state"] == 1

    # All subsequent warm reads are cache hits -> NO further delegate calls.
    latencies = await _timed_warm_reads(cache, "acct", WARM_READS)
    assert delegate.calls["get_account_state"] == 1, (
        "cache hits must not call the delegate"
    )

    p95 = _percentile(latencies, 0.95)
    avg = sum(latencies) / len(latencies)
    # Primary robustness comes from the call-count assertion above; the timing
    # assertions use a generous budget to avoid wall-clock flakiness.
    assert p95 < P95_BUDGET_SECONDS, f"warm cache-hit P95={p95 * 1000:.3f}ms exceeds budget"
    # A cache hit is pure memory, so it must be far cheaper than a delegate call.
    assert avg < SIMULATED_DELEGATE_LATENCY, (
        f"warm cache-hit avg={avg * 1000:.3f}ms not below delegate latency "
        f"{SIMULATED_DELEGATE_LATENCY * 1000:.1f}ms"
    )


@pytest.mark.asyncio
async def test_cache_hits_cover_sticky_index_and_model_mapping():
    """
    The sticky-index and model-mapping read paths are cached too: each is fetched
    from the delegate at most once within the TTL.

    Requirements: 11.3
    """
    delegate = _CountingProxy(_MemoryStore(), latency=SIMULATED_DELEGATE_LATENCY)
    await delegate.set_sticky_index(3)
    await delegate.add_model_account("m", "a1")
    delegate.calls.clear()  # only count reads issued through the cache below

    cache = CachingStateStore(delegate, ttl_seconds=5.0)

    for _ in range(50):
        assert await cache.get_sticky_index() == 3
    for _ in range(50):
        assert await cache.get_model_mapping("m") == ["a1"]

    assert delegate.calls["get_sticky_index"] == 1
    assert delegate.calls["get_model_mapping"] == 1


@pytest.mark.asyncio
async def test_write_through_keeps_reads_off_the_delegate():
    """
    ``increment_failure`` writes its post-update state through into the cache, so
    an immediately-following read is a cache hit and performs no delegate *read*
    call (write-through self-consistency on the writing instance).

    Requirements: 11.3
    """
    delegate = _CountingProxy(_MemoryStore(), latency=SIMULATED_DELEGATE_LATENCY)
    cache = CachingStateStore(delegate, ttl_seconds=5.0)

    updated = await cache.increment_failure("acct", failure_time=42.0)
    assert updated.failures == 1
    assert delegate.calls["increment_failure"] == 1

    # Reads after the write-through are cache hits: no delegate get_account_state.
    for _ in range(WARM_READS):
        state = await cache.get_account_state("acct")
        assert state.failures == 1
    assert delegate.calls["get_account_state"] == 0


@pytest.mark.asyncio
async def test_cache_hit_avoids_billed_dynamodb_call_moto():
    """
    The same benefit holds with a real moto-backed ``DynamoStateStore`` as the
    delegate: after the initial miss, warm reads are served from the cache and
    never reach the (billed) DynamoDB delegate, comfortably under the P95 budget.

    Requirements: 11.3
    """
    boto3 = pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto import mock_aws
    from kiro.backends.aws.state_store import PK_ATTR, SK_ATTR, DynamoStateStore

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
    os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

    region = "us-east-1"
    table_name = "kiro-perf-gateway-state"

    with mock_aws():
        dynamo_resource = boto3.resource("dynamodb", region_name=region)
        table = dynamo_resource.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": PK_ATTR, "KeyType": "HASH"},
                {"AttributeName": SK_ATTR, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": PK_ATTR, "AttributeType": "S"},
                {"AttributeName": SK_ATTR, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()

        # Real DynamoStateStore underneath a call-counting proxy, fronted by the
        # TTL cache. Built inside the running loop so its asyncio.Lock binds here.
        dynamo_store = DynamoStateStore(table=table)
        counting = _CountingProxy(dynamo_store)
        cache = CachingStateStore(counting, ttl_seconds=5.0)

        # Seed one account *directly on the delegate* so the read cache starts
        # cold (a write through the cache would pre-populate it and turn the
        # first read into a hit).
        await counting.increment_failure("acct", failure_time=7.0)
        counting.calls.clear()

        # First read misses -> one DynamoDB-backed delegate call.
        first = await cache.get_account_state("acct")
        assert first.failures == 1
        assert counting.calls["get_account_state"] == 1

        # Warm reads are cache hits -> no further (billed) delegate calls.
        latencies = await _timed_warm_reads(cache, "acct", WARM_READS)
        assert counting.calls["get_account_state"] == 1

    p95 = _percentile(latencies, 0.95)
    assert p95 < P95_BUDGET_SECONDS, f"warm cache-hit P95={p95 * 1000:.3f}ms exceeds budget"
