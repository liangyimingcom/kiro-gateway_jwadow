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
NoopCoordinator - token refresh coordinator for the local (single-instance)
form.

In a single process the existing ``asyncio.Lock`` in ``KiroAuthManager`` already
guarantees single-flight refresh, so this coordinator simply grants the lock and
keeps any refreshed token in memory for completeness of the
:class:`TokenRefreshCoordinator` protocol (Requirements 6.7).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Dict, Optional

from kiro.backends.interfaces import TokenBundle


class NoopCoordinator:
    """
    Single-instance token refresh coordinator.

    There is no distributed coordination to perform locally: ``acquire_lock``
    always succeeds (single instance == single flight). A per-account
    ``asyncio.Lock`` is still used so concurrent coroutines within the process
    serialise correctly, mirroring the in-process lock used today.
    """

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tokens: Dict[str, TokenBundle] = {}

    async def acquire_lock(self, account_id: str, ttl_seconds: int) -> bool:
        """
        Acquire the in-process lock for ``account_id``.

        Always returns ``True`` (the single instance is always the single
        flight). ``ttl_seconds`` is accepted for protocol compatibility but is
        not needed without distributed leases.
        """
        await self._locks[account_id].acquire()
        return True

    async def release_lock(self, account_id: str) -> None:
        """Release the in-process lock for ``account_id`` if held."""
        lock = self._locks[account_id]
        if lock.locked():
            lock.release()

    async def store_refreshed_token(self, account_id: str, token: TokenBundle) -> None:
        """Keep the refreshed token in memory (no external persistence locally)."""
        self._tokens[account_id] = token

    async def wait_for_token(self, account_id: str, timeout: float) -> Optional[TokenBundle]:
        """
        Return the most recently stored token for ``account_id``, if any.

        In a single instance there is never another instance to wait for, so this
        returns immediately.
        """
        return self._tokens.get(account_id)
