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
In-process TTL cache layer for the State_Store read path (read-latency
optimisation).

In the cloud-native form every Gateway instance reads shared runtime state
(account failure counters, sticky index, model mappings) from a remote, billed
store (DynamoDB). Issuing a network ``GetItem`` on every account-selection read
would (a) add latency to non-streaming requests and (b) cost one billed call per
read. design.md "数据模型 → 本地 TTL 缓存层" prescribes a small per-instance TTL
cache in front of those reads:

- **Read**: a cache hit returns immediately and avoids the external call; a miss
  (or an expired entry) falls through to the wrapped store and refills the cache.
- **Write**: writes are *write-through* - they update or invalidate the relevant
  cache entry so subsequent reads on the **same instance** stay self-consistent
  (Requirements 11.5).
- **Goal**: keep the extra latency that reading shared state/config/secrets adds
  to a non-streaming request at P95 ≤ 50ms (Requirements 11.3) while reducing the
  number of billed store calls (Requirements 12.2).
- **Consistency trade-off**: cross-instance visibility is eventually consistent
  (within the TTL, ~1-2s). Failover / circuit-breaker decisions tolerate this
  small staleness by design (probabilistic retry and half-open behaviour are
  themselves fault-tolerant).

This module provides two pieces:

1. :class:`StateCache` - a generic, thread-safe, monotonic-clock TTL cache. It is
   deliberately storage-agnostic so it can front any read path (state, config or
   secret) that benefits from short-lived memoisation.
2. :class:`CachingStateStore` - a transparent decorator that implements the
   :class:`~kiro.backends.interfaces.StateStore` protocol by wrapping any other
   ``StateStore`` (notably
   :class:`~kiro.backends.aws.state_store.DynamoStateStore`) with the TTL read
   cache plus write-through invalidation.

The local file backend is already an in-memory store, so wrapping it is optional
(its reads do not hit the network); the factory wraps the AWS backend where the
cache provides the latency / cost benefit.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Generic, List, Optional, TypeVar

from kiro.backends.interfaces import AccountState, AccountStatsState, StateStore

__all__ = [
    "StateCache",
    "CachingStateStore",
    "DEFAULT_STATE_CACHE_TTL_SECONDS",
    "STATE_CACHE_TTL_ENV",
]

# Environment variable controlling the State_Store read-cache TTL (seconds).
STATE_CACHE_TTL_ENV = "STATE_CACHE_TTL_SECONDS"

# Default TTL for the State_Store read cache. design.md specifies ~1-2s; we use
# the upper end of that window as a sensible default (favouring fewer billed
# calls) while staying well within the freshness the failover logic tolerates.
DEFAULT_STATE_CACHE_TTL_SECONDS = 2.0


T = TypeVar("T")


# ==================================================================================================
# Generic TTL cache
# ==================================================================================================


class StateCache(Generic[T]):
    """
    A small, thread-safe, in-process TTL cache.

    Entries expire ``ttl_seconds`` after they are written, measured on a
    monotonic clock so the cache is immune to wall-clock adjustments. A TTL of
    ``0`` (or negative) disables caching entirely - every :meth:`get` misses and
    :meth:`put` is a no-op - which is handy for tests or for opting out without
    branching at every call-site.

    The cache is intentionally generic and storage-agnostic: keys are arbitrary
    strings and values are arbitrary objects. Callers are responsible for storing
    immutable values or defensive copies when they intend to mutate the result
    (see :class:`CachingStateStore`, which copies :class:`AccountState` on the
    way in and out).
    """

    def __init__(self, ttl_seconds: float = DEFAULT_STATE_CACHE_TTL_SECONDS) -> None:
        """
        Args:
            ttl_seconds: Time-to-live for each entry, in seconds. ``<= 0``
                disables caching.
        """
        self._ttl = float(ttl_seconds)
        self._store: dict[str, tuple[T, float]] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> float:
        """The configured TTL in seconds (``<= 0`` means caching is disabled)."""
        return self._ttl

    @property
    def enabled(self) -> bool:
        """Whether caching is active (i.e. the TTL is positive)."""
        return self._ttl > 0

    def get(self, key: str) -> Optional[T]:
        """
        Return the cached value for ``key`` or ``None`` if absent/expired.

        Expired entries are evicted lazily on access so the cache never serves a
        stale value beyond its TTL.
        """
        if self._ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at <= now:
                # Expired - evict so the caller refills from the source of truth.
                self._store.pop(key, None)
                return None
            return value

    def put(self, key: str, value: T) -> None:
        """Store ``value`` under ``key`` with a fresh TTL (no-op if disabled)."""
        if self._ttl <= 0:
            return
        with self._lock:
            self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key: Optional[str] = None) -> None:
        """
        Evict cached entries.

        With no argument the whole cache is cleared; otherwise only ``key`` is
        removed. Invalidating an absent key is a no-op.
        """
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)

    def __len__(self) -> int:  # pragma: no cover - introspection helper
        with self._lock:
            return len(self._store)


# ==================================================================================================
# Caching StateStore decorator
# ==================================================================================================


def _copy_account_state(state: AccountState) -> AccountState:
    """Return a deep-enough copy so cached state can never be mutated in place."""
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


# Cache-key namespaces keep the different read paths from colliding inside the
# single underlying StateCache instance.
_ACCOUNT_KEY_PREFIX = "account:"
_MODEL_KEY_PREFIX = "model:"
_STICKY_KEY = "sticky-index"


class CachingStateStore:
    """
    A transparent TTL read-cache + write-through decorator for any
    :class:`~kiro.backends.interfaces.StateStore`.

    Wrapping (for example) :class:`~kiro.backends.aws.state_store.DynamoStateStore`
    means account-selection reads (``get_account_state``, ``get_sticky_index``,
    ``get_model_mapping``) are served from process memory within the TTL,
    cutting both per-request latency (Requirements 11.3) and billed store calls
    (Requirements 12.2). Writes update or invalidate the corresponding entry so
    that, on the writing instance, an immediately-following read observes the
    write (Requirements 11.5):

    - ``increment_failure`` returns the post-update :class:`AccountState`, which
      is written through into the cache.
    - ``reset_failure`` / ``incr_stats`` mutate the cached state when present so
      reads stay self-consistent without an extra round-trip; if nothing is
      cached the next read simply repopulates from the store.
    - ``set_sticky_index`` is written through.
    - ``add_model_account`` invalidates the affected model entry (the next read
      re-fetches the authoritative, de-duplicated set).

    The decorator implements the full ``StateStore`` protocol and forwards any
    non-protocol attribute to the wrapped store, so it never breaks the contract
    a consumer relies on.
    """

    def __init__(
        self,
        delegate: StateStore,
        *,
        ttl_seconds: Optional[float] = None,
        cache: Optional[StateCache] = None,
    ) -> None:
        """
        Args:
            delegate: The underlying :class:`StateStore` to wrap (e.g. a
                ``DynamoStateStore``).
            ttl_seconds: TTL for the read cache. When ``None`` it is resolved from
                the ``STATE_CACHE_TTL_SECONDS`` environment variable, then from
                :data:`DEFAULT_STATE_CACHE_TTL_SECONDS`. Ignored when ``cache`` is
                provided.
            cache: An explicit :class:`StateCache` instance (primarily for tests
                / dependency injection). When provided it is used as-is.
        """
        self._delegate = delegate
        if cache is not None:
            self._cache: StateCache = cache
        else:
            self._cache = StateCache(self._resolve_ttl(ttl_seconds))

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_ttl(ttl_seconds: Optional[float]) -> float:
        """Resolve the TTL from the argument, environment, then the default."""
        if ttl_seconds is not None:
            return float(ttl_seconds)
        raw = os.getenv(STATE_CACHE_TTL_ENV)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return DEFAULT_STATE_CACHE_TTL_SECONDS

    @property
    def delegate(self) -> StateStore:
        """The wrapped :class:`StateStore`."""
        return self._delegate

    @property
    def cache(self) -> StateCache:
        """The underlying :class:`StateCache` (exposed for diagnostics / tests)."""
        return self._cache

    # ------------------------------------------------------------------
    # Internal cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _account_key(account_id: str) -> str:
        return f"{_ACCOUNT_KEY_PREFIX}{account_id}"

    @staticmethod
    def _model_key(model: str) -> str:
        return f"{_MODEL_KEY_PREFIX}{model}"

    def _cache_account(self, state: AccountState) -> None:
        """Write a defensive copy of ``state`` into the cache (write-through)."""
        self._cache.put(self._account_key(state.account_id), _copy_account_state(state))

    def _cached_account(self, account_id: str) -> Optional[AccountState]:
        """Return a cached copy of the account state, if fresh."""
        cached = self._cache.get(self._account_key(account_id))
        if cached is None:
            return None
        # Return a copy so callers cannot mutate the cached instance.
        return _copy_account_state(cached)

    # ------------------------------------------------------------------
    # StateStore protocol - reads (cache-first)
    # ------------------------------------------------------------------

    async def get_account_state(self, account_id: str) -> AccountState:
        """
        Return the shared state for ``account_id``, served from cache when fresh.

        On a miss a single read is dispatched to the wrapped store and the result
        is cached for the configured TTL.
        """
        cached = self._cached_account(account_id)
        if cached is not None:
            return cached
        state = await self._delegate.get_account_state(account_id)
        self._cache_account(state)
        # Return a copy so the cached instance stays isolated from the caller.
        return _copy_account_state(state)

    async def get_sticky_index(self) -> int:
        """Return the global sticky index, served from cache when fresh."""
        cached = self._cache.get(_STICKY_KEY)
        if cached is not None:
            return int(cached)
        index = await self._delegate.get_sticky_index()
        self._cache.put(_STICKY_KEY, int(index))
        return index

    async def get_model_mapping(self, model: str) -> List[str]:
        """Return the account list for ``model``, served from cache when fresh."""
        key = self._model_key(model)
        cached = self._cache.get(key)
        if cached is not None:
            # Return a copy so the caller cannot mutate the cached list.
            return list(cached)
        accounts = await self._delegate.get_model_mapping(model)
        self._cache.put(key, list(accounts))
        return list(accounts)

    # ------------------------------------------------------------------
    # StateStore protocol - writes (write-through / invalidate)
    # ------------------------------------------------------------------

    async def increment_failure(self, account_id: str, failure_time: float) -> AccountState:
        """
        Atomically increment the failure counter on the wrapped store and write
        the returned post-update state through into the cache.
        """
        state = await self._delegate.increment_failure(account_id, failure_time)
        self._cache_account(state)
        return _copy_account_state(state)

    async def reset_failure(self, account_id: str) -> None:
        """
        Reset the failure counter on the wrapped store and keep the cache
        self-consistent (set the cached ``failures`` to ``0`` when present).
        """
        await self._delegate.reset_failure(account_id)
        cached = self._cache.get(self._account_key(account_id))
        if cached is not None:
            updated = _copy_account_state(cached)
            updated.failures = 0
            self._cache.put(self._account_key(account_id), updated)

    async def incr_stats(self, account_id: str, *, total: int, ok: int, failed: int) -> None:
        """
        Increment usage statistics on the wrapped store and apply the same
        deltas to the cached state when present (keeping reads self-consistent).
        """
        await self._delegate.incr_stats(account_id, total=total, ok=ok, failed=failed)
        cached = self._cache.get(self._account_key(account_id))
        if cached is not None:
            updated = _copy_account_state(cached)
            updated.stats.total_requests += total
            updated.stats.successful_requests += ok
            updated.stats.failed_requests += failed
            self._cache.put(self._account_key(account_id), updated)

    async def set_sticky_index(self, index: int) -> None:
        """Set the sticky index on the wrapped store and write it through."""
        await self._delegate.set_sticky_index(index)
        self._cache.put(_STICKY_KEY, int(index))

    async def add_model_account(self, model: str, account_id: str) -> None:
        """
        Idempotently add ``account_id`` to ``model`` on the wrapped store and
        invalidate the cached mapping so the next read reflects the change.
        """
        await self._delegate.add_model_account(model, account_id)
        self._cache.invalidate(self._model_key(model))

    # ------------------------------------------------------------------
    # Transparent delegation for any non-protocol attributes
    # ------------------------------------------------------------------

    def __getattr__(self, name: str):
        """
        Forward unknown attributes to the wrapped store.

        This keeps the decorator transparent for backend-specific helpers that
        are not part of the :class:`StateStore` protocol (e.g. the local
        backend's ``load`` / ``flush`` / ``save_state_periodically``). Only
        attributes not defined on this class reach here, so the cached protocol
        methods above always take precedence.
        """
        # ``self._delegate`` is set in __init__; guard against lookups during
        # unpickling / partial construction to avoid infinite recursion.
        delegate = self.__dict__.get("_delegate")
        if delegate is None:
            raise AttributeError(name)
        return getattr(delegate, name)
