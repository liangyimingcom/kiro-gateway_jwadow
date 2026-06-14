# -*- coding: utf-8 -*-

"""
Unit tests for :mod:`kiro.backends.cache_layer`.

These tests exercise the in-process TTL cache (:class:`StateCache`) and the
write-through read-cache decorator (:class:`CachingStateStore`) against a
lightweight in-memory ``StateStore`` double that counts the calls reaching it.
The double stands in for the *external* store (e.g. DynamoDB); the cache logic
under test is real, not mocked.
"""

import asyncio

import pytest

from kiro.backends.cache_layer import (
    DEFAULT_STATE_CACHE_TTL_SECONDS,
    STATE_CACHE_TTL_ENV,
    CachingStateStore,
    StateCache,
)
from kiro.backends.interfaces import AccountState, AccountStatsState, StateStore


# =============================================================================
# In-memory StateStore double (counts delegate calls)
# =============================================================================


class CountingStateStore:
    """Minimal in-memory ``StateStore`` that records how often it is hit."""

    def __init__(self):
        self.accounts: dict[str, AccountState] = {}
        self.sticky_index = 0
        self.models: dict[str, list[str]] = {}
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def _get_or_create(self, account_id: str) -> AccountState:
        state = self.accounts.get(account_id)
        if state is None:
            state = AccountState(account_id=account_id)
            self.accounts[account_id] = state
        return state

    async def get_account_state(self, account_id: str) -> AccountState:
        self._count("get_account_state")
        return self._get_or_create(account_id)

    async def increment_failure(self, account_id: str, failure_time: float) -> AccountState:
        self._count("increment_failure")
        state = self._get_or_create(account_id)
        state.failures += 1
        state.last_failure_time = failure_time
        return state

    async def reset_failure(self, account_id: str) -> None:
        self._count("reset_failure")
        self._get_or_create(account_id).failures = 0

    async def incr_stats(self, account_id: str, *, total: int, ok: int, failed: int) -> None:
        self._count("incr_stats")
        state = self._get_or_create(account_id)
        state.stats.total_requests += total
        state.stats.successful_requests += ok
        state.stats.failed_requests += failed

    async def get_sticky_index(self) -> int:
        self._count("get_sticky_index")
        return self.sticky_index

    async def set_sticky_index(self, index: int) -> None:
        self._count("set_sticky_index")
        self.sticky_index = index

    async def get_model_mapping(self, model: str) -> list[str]:
        self._count("get_model_mapping")
        return list(self.models.get(model, []))

    async def add_model_account(self, model: str, account_id: str) -> None:
        self._count("add_model_account")
        accounts = self.models.setdefault(model, [])
        if account_id not in accounts:
            accounts.append(account_id)

    # A non-protocol helper, used to verify transparent delegation.
    def backend_specific_helper(self) -> str:
        return "delegated"


# =============================================================================
# StateCache - generic TTL cache
# =============================================================================


class TestStateCache:
    def test_put_and_get_returns_value_within_ttl(self):
        cache: StateCache = StateCache(ttl_seconds=10.0)
        cache.put("k", 123)
        assert cache.get("k") == 123

    def test_missing_key_returns_none(self):
        cache: StateCache = StateCache(ttl_seconds=10.0)
        assert cache.get("absent") is None

    def test_entry_expires_after_ttl(self):
        cache: StateCache = StateCache(ttl_seconds=0.05)
        cache.put("k", "v")
        assert cache.get("k") == "v"
        import time

        time.sleep(0.08)
        assert cache.get("k") is None

    def test_zero_ttl_disables_caching(self):
        cache: StateCache = StateCache(ttl_seconds=0)
        cache.put("k", "v")
        assert cache.get("k") is None
        assert cache.enabled is False

    def test_negative_ttl_disables_caching(self):
        cache: StateCache = StateCache(ttl_seconds=-5)
        cache.put("k", "v")
        assert cache.get("k") is None

    def test_invalidate_single_key(self):
        cache: StateCache = StateCache(ttl_seconds=10.0)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.invalidate("a")
        assert cache.get("a") is None
        assert cache.get("b") == 2

    def test_invalidate_all(self):
        cache: StateCache = StateCache(ttl_seconds=10.0)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.invalidate()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_ttl_property_reports_configured_value(self):
        assert StateCache(ttl_seconds=1.5).ttl_seconds == 1.5
        assert StateCache(ttl_seconds=3).enabled is True


# =============================================================================
# CachingStateStore - protocol conformance & construction
# =============================================================================


class TestCachingStateStoreConstruction:
    def test_satisfies_state_store_protocol(self):
        store = CachingStateStore(CountingStateStore(), ttl_seconds=10.0)
        assert isinstance(store, StateStore)

    def test_ttl_resolved_from_env(self, monkeypatch):
        monkeypatch.setenv(STATE_CACHE_TTL_ENV, "7.5")
        store = CachingStateStore(CountingStateStore())
        assert store.cache.ttl_seconds == 7.5

    def test_ttl_default_when_env_absent(self, monkeypatch):
        monkeypatch.delenv(STATE_CACHE_TTL_ENV, raising=False)
        store = CachingStateStore(CountingStateStore())
        assert store.cache.ttl_seconds == DEFAULT_STATE_CACHE_TTL_SECONDS

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(STATE_CACHE_TTL_ENV, "not-a-number")
        store = CachingStateStore(CountingStateStore())
        assert store.cache.ttl_seconds == DEFAULT_STATE_CACHE_TTL_SECONDS

    def test_explicit_ttl_overrides_env(self, monkeypatch):
        monkeypatch.setenv(STATE_CACHE_TTL_ENV, "7.5")
        store = CachingStateStore(CountingStateStore(), ttl_seconds=1.0)
        assert store.cache.ttl_seconds == 1.0

    def test_delegate_property_exposes_wrapped_store(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)
        assert store.delegate is delegate

    def test_transparent_delegation_for_non_protocol_attrs(self):
        store = CachingStateStore(CountingStateStore(), ttl_seconds=10.0)
        assert store.backend_specific_helper() == "delegated"

    def test_unknown_attribute_raises(self):
        store = CachingStateStore(CountingStateStore(), ttl_seconds=10.0)
        with pytest.raises(AttributeError):
            _ = store.does_not_exist


# =============================================================================
# CachingStateStore - read caching
# =============================================================================


class TestCachingStateStoreReads:
    @pytest.mark.asyncio
    async def test_account_state_cache_hit_avoids_delegate(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        first = await store.get_account_state("acct-1")
        second = await store.get_account_state("acct-1")

        assert first.account_id == "acct-1"
        assert second.account_id == "acct-1"
        # Only the first read reaches the delegate.
        assert delegate.calls.get("get_account_state") == 1

    @pytest.mark.asyncio
    async def test_account_state_refetched_after_expiry(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=0.05)

        await store.get_account_state("acct-1")
        await asyncio.sleep(0.08)
        await store.get_account_state("acct-1")

        assert delegate.calls.get("get_account_state") == 2

    @pytest.mark.asyncio
    async def test_account_state_disabled_cache_always_hits_delegate(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=0)

        await store.get_account_state("acct-1")
        await store.get_account_state("acct-1")

        assert delegate.calls.get("get_account_state") == 2

    @pytest.mark.asyncio
    async def test_returned_state_is_isolated_copy(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        first = await store.get_account_state("acct-1")
        first.failures = 999  # mutate the caller's copy

        second = await store.get_account_state("acct-1")
        assert second.failures == 0  # cache not corrupted

    @pytest.mark.asyncio
    async def test_sticky_index_cache_hit_avoids_delegate(self):
        delegate = CountingStateStore()
        delegate.sticky_index = 3
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        assert await store.get_sticky_index() == 3
        assert await store.get_sticky_index() == 3
        assert delegate.calls.get("get_sticky_index") == 1

    @pytest.mark.asyncio
    async def test_model_mapping_cache_hit_avoids_delegate(self):
        delegate = CountingStateStore()
        delegate.models["m"] = ["a", "b"]
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        assert await store.get_model_mapping("m") == ["a", "b"]
        assert await store.get_model_mapping("m") == ["a", "b"]
        assert delegate.calls.get("get_model_mapping") == 1

    @pytest.mark.asyncio
    async def test_model_mapping_returns_isolated_copy(self):
        delegate = CountingStateStore()
        delegate.models["m"] = ["a"]
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        result = await store.get_model_mapping("m")
        result.append("mutated")

        again = await store.get_model_mapping("m")
        assert again == ["a"]


# =============================================================================
# CachingStateStore - write-through / invalidation
# =============================================================================


class TestCachingStateStoreWrites:
    @pytest.mark.asyncio
    async def test_increment_failure_is_written_through(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        await store.increment_failure("acct-1", failure_time=123.0)
        # A subsequent read must reflect the write without hitting the delegate.
        state = await store.get_account_state("acct-1")

        assert state.failures == 1
        assert state.last_failure_time == 123.0
        assert delegate.calls.get("get_account_state") is None

    @pytest.mark.asyncio
    async def test_reset_failure_keeps_cache_self_consistent(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        await store.increment_failure("acct-1", failure_time=5.0)
        await store.reset_failure("acct-1")
        state = await store.get_account_state("acct-1")

        assert state.failures == 0
        # Read still served from cache (no delegate read needed).
        assert delegate.calls.get("get_account_state") is None

    @pytest.mark.asyncio
    async def test_incr_stats_updates_cached_state(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        # Prime the cache so incr_stats has something to update.
        await store.get_account_state("acct-1")
        await store.incr_stats("acct-1", total=2, ok=1, failed=1)
        state = await store.get_account_state("acct-1")

        assert state.stats.total_requests == 2
        assert state.stats.successful_requests == 1
        assert state.stats.failed_requests == 1
        # Both reads were served from cache after the initial priming read.
        assert delegate.calls.get("get_account_state") == 1

    @pytest.mark.asyncio
    async def test_incr_stats_without_cached_entry_repopulates_from_store(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        # No prior read -> nothing cached. incr_stats updates the delegate only.
        await store.incr_stats("acct-1", total=3, ok=3, failed=0)
        state = await store.get_account_state("acct-1")

        assert state.stats.total_requests == 3
        assert delegate.calls.get("get_account_state") == 1

    @pytest.mark.asyncio
    async def test_set_sticky_index_is_written_through(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        await store.set_sticky_index(7)
        assert await store.get_sticky_index() == 7
        # The write-through means no read reaches the delegate.
        assert delegate.calls.get("get_sticky_index") is None
        assert delegate.sticky_index == 7

    @pytest.mark.asyncio
    async def test_add_model_account_invalidates_cache(self):
        delegate = CountingStateStore()
        delegate.models["m"] = ["a"]
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        # Prime the cache.
        assert await store.get_model_mapping("m") == ["a"]
        # Mutation invalidates the entry so the next read re-fetches.
        await store.add_model_account("m", "b")
        assert await store.get_model_mapping("m") == ["a", "b"]
        assert delegate.calls.get("get_model_mapping") == 2

    @pytest.mark.asyncio
    async def test_increment_failure_returns_isolated_copy(self):
        delegate = CountingStateStore()
        store = CachingStateStore(delegate, ttl_seconds=10.0)

        returned = await store.increment_failure("acct-1", failure_time=1.0)
        returned.failures = 555  # mutate caller copy

        cached = await store.get_account_state("acct-1")
        assert cached.failures == 1


# =============================================================================
# Factory wiring
# =============================================================================


class TestFactoryWiring:
    def test_aws_bundle_wraps_state_store_with_caching(self):
        from kiro.backends import factory

        bundle = factory.create_backend("aws")
        assert isinstance(bundle.state_store, CachingStateStore)
        # The wrapped delegate must be the DynamoStateStore.
        from kiro.backends.aws.state_store import DynamoStateStore

        assert isinstance(bundle.state_store.delegate, DynamoStateStore)
