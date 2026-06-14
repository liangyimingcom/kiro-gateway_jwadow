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
LocalStateStore - shared runtime state store backed by ``state.json``.

Implements the :class:`StateStore` protocol with the same semantics the gateway
has today: in-memory state, atomic ``tmp + rename`` persistence and periodic
(~10s) flush to disk. This keeps the local (development) form behaviourally
equivalent to the current ``AccountManager`` state handling
(Requirements 1.4, 7.1, 7.2).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List

from loguru import logger

from kiro.backends.interfaces import AccountState, AccountStatsState
from kiro.config import STATE_SAVE_INTERVAL_SECONDS


class LocalStateStore:
    """
    File-backed shared state store.

    State layout on disk mirrors the existing ``state.json`` structure::

        {
          "current_account_index": <int>,
          "accounts": { "<id>": { failures, last_failure_time, models_cached_at, stats {...} } },
          "model_to_accounts": { "<model>": { "accounts": [ "<id>", ... ] } }
        }
    """

    def __init__(self, state_file: str) -> None:
        """
        Args:
            state_file: Path to ``state.json``.
        """
        self._state_file = state_file
        self._accounts: Dict[str, AccountState] = {}
        self._model_to_accounts: Dict[str, List[str]] = {}
        self._sticky_index: int = 0
        self._lock = asyncio.Lock()
        self._dirty = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """
        Load runtime state from ``state.json``.

        Creates empty state if the file does not exist (matching current
        behaviour).
        """
        state_path = Path(self._state_file)
        if not state_path.exists():
            logger.debug("State file not found, starting with empty state")
            return

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            return

        async with self._lock:
            self._sticky_index = state_data.get("current_account_index", 0)

            for model, data in state_data.get("model_to_accounts", {}).items():
                self._model_to_accounts[model] = list(data.get("accounts", []))

            for account_id, data in state_data.get("accounts", {}).items():
                stats_data = data.get("stats", {})
                self._accounts[account_id] = AccountState(
                    account_id=account_id,
                    failures=data.get("failures", 0),
                    last_failure_time=data.get("last_failure_time", 0.0),
                    models_cached_at=data.get("models_cached_at", 0.0),
                    stats=AccountStatsState(
                        total_requests=stats_data.get("total_requests", 0),
                        successful_requests=stats_data.get("successful_requests", 0),
                        failed_requests=stats_data.get("failed_requests", 0),
                    ),
                )

        logger.info(
            f"Loaded state: {len(self._model_to_accounts)} model mappings, "
            f"{len(self._accounts)} accounts"
        )

    def _serialize(self) -> dict:
        """Build the on-disk representation of the current state."""
        return {
            "current_account_index": self._sticky_index,
            "accounts": {
                account_id: {
                    "failures": state.failures,
                    "last_failure_time": state.last_failure_time,
                    "models_cached_at": state.models_cached_at,
                    "stats": {
                        "total_requests": state.stats.total_requests,
                        "successful_requests": state.stats.successful_requests,
                        "failed_requests": state.stats.failed_requests,
                    },
                }
                for account_id, state in self._accounts.items()
            },
            "model_to_accounts": {
                model: {"accounts": list(accounts)}
                for model, accounts in self._model_to_accounts.items()
            },
        }

    async def _save_locked(self) -> None:
        """Persist state atomically (caller must hold ``self._lock``)."""
        state_data = self._serialize()
        state_path = Path(self._state_file)
        tmp_path = state_path.with_suffix(".json.tmp")

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)
            # Atomic rename
            tmp_path.replace(state_path)
            logger.debug("State saved successfully")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            if tmp_path.exists():
                tmp_path.unlink()

    async def flush(self) -> None:
        """Force an immediate persistence of the current state."""
        async with self._lock:
            await self._save_locked()
            self._dirty = False

    async def save_state_periodically(self) -> None:
        """
        Background task that persists state every
        ``STATE_SAVE_INTERVAL_SECONDS`` seconds when it has changed.
        """
        while True:
            await asyncio.sleep(STATE_SAVE_INTERVAL_SECONDS)
            if self._dirty:
                async with self._lock:
                    await self._save_locked()
                    self._dirty = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create_locked(self, account_id: str) -> AccountState:
        """Return the state for ``account_id``, creating empty state if needed."""
        state = self._accounts.get(account_id)
        if state is None:
            state = AccountState(account_id=account_id)
            self._accounts[account_id] = state
        return state

    @staticmethod
    def _copy_state(state: AccountState) -> AccountState:
        """Return a defensive copy so callers cannot mutate internal state."""
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

    # ------------------------------------------------------------------
    # StateStore protocol
    # ------------------------------------------------------------------

    async def get_account_state(self, account_id: str) -> AccountState:
        """Return a copy of the current shared state for ``account_id``."""
        async with self._lock:
            state = self._get_or_create_locked(account_id)
            return self._copy_state(state)

    async def increment_failure(self, account_id: str, failure_time: float) -> AccountState:
        """Atomically increment the failure counter and record the failure time."""
        async with self._lock:
            state = self._get_or_create_locked(account_id)
            state.failures += 1
            state.last_failure_time = failure_time
            self._dirty = True
            return self._copy_state(state)

    async def reset_failure(self, account_id: str) -> None:
        """Reset the failure counter for ``account_id`` to zero (success path)."""
        async with self._lock:
            state = self._get_or_create_locked(account_id)
            if state.failures != 0:
                state.failures = 0
                self._dirty = True

    async def incr_stats(self, account_id: str, *, total: int, ok: int, failed: int) -> None:
        """Atomically increment usage statistics counters."""
        async with self._lock:
            state = self._get_or_create_locked(account_id)
            state.stats.total_requests += total
            state.stats.successful_requests += ok
            state.stats.failed_requests += failed
            self._dirty = True

    async def get_sticky_index(self) -> int:
        """Return the global sticky account index."""
        async with self._lock:
            return self._sticky_index

    async def set_sticky_index(self, index: int) -> None:
        """Set the global sticky account index."""
        async with self._lock:
            if self._sticky_index != index:
                self._sticky_index = index
                self._dirty = True

    async def get_model_mapping(self, model: str) -> list[str]:
        """Return a copy of the account list known to serve ``model``."""
        async with self._lock:
            return list(self._model_to_accounts.get(model, []))

    async def add_model_account(self, model: str, account_id: str) -> None:
        """Idempotently add ``account_id`` to the account list for ``model``."""
        async with self._lock:
            accounts = self._model_to_accounts.setdefault(model, [])
            if account_id not in accounts:
                accounts.append(account_id)
                self._dirty = True

    # ------------------------------------------------------------------
    # Bulk helpers (local backend only)
    #
    # These are *not* part of the StateStore protocol. They exist so the
    # AccountManager can restore / persist the full local ``state.json`` snapshot
    # with exactly the same on-disk format as before. The AWS backend rebuilds
    # shared state lazily from an empty store instead (see design.md migration
    # step 5), so it does not provide these methods.
    # ------------------------------------------------------------------

    def export_all(self) -> tuple:
        """
        Return a snapshot ``(sticky_index, accounts, model_to_accounts)`` of the
        currently loaded state.

        ``accounts`` maps account id -> :class:`AccountState` and
        ``model_to_accounts`` maps model -> list of account ids.
        """
        accounts = {aid: self._copy_state(state) for aid, state in self._accounts.items()}
        models = {model: list(accs) for model, accs in self._model_to_accounts.items()}
        return self._sticky_index, accounts, models

    async def replace_all(
        self,
        sticky_index: int,
        accounts: Dict[str, AccountState],
        model_to_accounts: Dict[str, List[str]],
    ) -> None:
        """
        Replace the entire in-memory state with the provided snapshot.

        Used by :class:`~kiro.account_manager.AccountManager` to push its
        authoritative in-memory view into the store immediately before an atomic
        flush, preserving the historical ``state.json`` semantics exactly.
        """
        async with self._lock:
            self._sticky_index = sticky_index
            self._accounts = {
                aid: self._copy_state(state) for aid, state in accounts.items()
            }
            self._model_to_accounts = {
                model: list(accs) for model, accs in model_to_accounts.items()
            }
            self._dirty = True
