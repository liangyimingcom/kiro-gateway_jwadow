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
DynamoStateStore - shared runtime state store backed by DynamoDB.

Implements the :class:`~kiro.backends.interfaces.StateStore` protocol on top of
a DynamoDB **single-table design** so that all Gateway instances in the池
(ECS Fargate Service) make consistent failover decisions from one authoritative
store, using server-side **atomic** updates to avoid lost writes under
concurrency (Requirements 7.1, 7.2, 7.3, 7.5, 7.6, 3.5).

Single-table layout (see design.md "数据模型 → State_Store(DynamoDB) 表设计"):

    | 实体             | PK                    | SK         | 关键属性                                   |
    |------------------|-----------------------|------------|--------------------------------------------|
    | 账号状态         | ``ACCT#<account_id>`` | ``STATE``  | failures, last_failure_time, models_cached_at, stats |
    | 全局 sticky 索引 | ``GLOBAL``            | ``STICKY`` | sticky_index                               |
    | 模型→账号映射    | ``MODEL#<model>``     | ``ACCOUNTS`` | accounts (String Set)                    |
    | 令牌元数据       | ``TOKEN#<account_id>``| ``META``   | expires_at (access token 真值存于 Secrets Manager) |

Atomic update strategy:

- **失败计数**: ``UpdateItem`` with ``SET last_failure_time = :t ADD failures :one``
  (``ADD`` is a server-side atomic increment - concurrent instances never
  overwrite each other, Requirements 7.6).
- **统计自增**: ``ADD total_requests :t, successful_requests :o, failed_requests :f``.
- **重置失败**: ``SET failures = :zero`` (success path).
- **sticky 索引**: ``SET sticky_index = :idx`` (last-writer-wins is acceptable -
  sticky is only a soft preference).
- **模型映射追加**: ``ADD accounts :set`` using a DynamoDB **String Set** so the
  append is idempotent / de-duplicated.

Design notes:

- ``boto3`` is imported **lazily** (inside :meth:`_get_table`) so importing this
  module never requires ``boto3`` to be installed nor AWS credentials to be
  configured. This keeps the abstraction layer importable everywhere and lets
  the local backend and the rest of the test-suite run without AWS deps.
- The underlying ``boto3`` resource API is synchronous; the protocol is async,
  so every call is dispatched to a worker thread via :func:`asyncio.to_thread`
  to avoid blocking the event loop.
- The table name is configurable and defaults to ``<stack>-gateway-state``
  (design.md), where ``<stack>`` comes from the constructor or the
  ``GATEWAY_STACK_NAME`` environment variable.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

from loguru import logger

from kiro.backends.interfaces import AccountState, AccountStatsState

# --------------------------------------------------------------------------------------------------
# Single-table key constants
# --------------------------------------------------------------------------------------------------

#: Partition-key attribute name.
PK_ATTR = "PK"
#: Sort-key attribute name.
SK_ATTR = "SK"

#: DynamoDB TTL attribute (auto-clears expired / archival items, e.g. token metadata).
TTL_ATTR = "expires_at"

# Entity prefixes / fixed sort keys.
_ACCT_PREFIX = "ACCT#"
_STATE_SK = "STATE"
_GLOBAL_PK = "GLOBAL"
_STICKY_SK = "STICKY"
_MODEL_PREFIX = "MODEL#"
_ACCOUNTS_SK = "ACCOUNTS"
_TOKEN_PREFIX = "TOKEN#"
_META_SK = "META"

# Default table-name template (design.md: ``<stack>-gateway-state``).
_DEFAULT_STACK = "kiro-gateway"
_TABLE_SUFFIX = "-gateway-state"


def _account_state_key(account_id: str) -> Dict[str, str]:
    """Primary key for the per-account shared-state item."""
    return {PK_ATTR: f"{_ACCT_PREFIX}{account_id}", SK_ATTR: _STATE_SK}


def _sticky_key() -> Dict[str, str]:
    """Primary key for the global sticky-index item."""
    return {PK_ATTR: _GLOBAL_PK, SK_ATTR: _STICKY_SK}


def _model_key(model: str) -> Dict[str, str]:
    """Primary key for the model -> accounts mapping item."""
    return {PK_ATTR: f"{_MODEL_PREFIX}{model}", SK_ATTR: _ACCOUNTS_SK}


def _token_meta_key(account_id: str) -> Dict[str, str]:
    """Primary key for the per-account token-metadata item."""
    return {PK_ATTR: f"{_TOKEN_PREFIX}{account_id}", SK_ATTR: _META_SK}


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a DynamoDB number (``Decimal``) to ``int``."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce a DynamoDB number (``Decimal``) to ``float``."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class DynamoStateStore:
    """
    DynamoDB-backed implementation of the :class:`StateStore` protocol.

    Args:
        table_name: Explicit DynamoDB table name. When ``None`` the name is
            derived as ``<stack>-gateway-state`` (see ``stack``).
        stack: Stack / deployment name used to derive the table name when
            ``table_name`` is not given. Falls back to the ``GATEWAY_STACK_NAME``
            environment variable and then to ``"kiro-gateway"``.
        region_name: AWS region. Falls back to the standard ``AWS_REGION`` /
            ``AWS_DEFAULT_REGION`` resolution performed by ``boto3``.
        endpoint_url: Optional custom endpoint (e.g. for LocalStack / moto in
            tests).
        table: Pre-built ``boto3`` DynamoDB ``Table`` resource. When supplied it
            is used as-is (primarily for testing / dependency injection) and no
            lazy client is created.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        *,
        stack: Optional[str] = None,
        region_name: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        table: Any = None,
    ) -> None:
        self._table_name = table_name or self._resolve_table_name(stack)
        self._region_name = region_name
        self._endpoint_url = endpoint_url
        # Lazily initialised boto3 Table resource (or an injected one).
        self._table = table
        self._init_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lazy client / table initialisation
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_table_name(stack: Optional[str]) -> str:
        """Derive ``<stack>-gateway-state`` from the argument or environment."""
        # Allow a fully explicit override via env for operational flexibility.
        explicit = os.getenv("GATEWAY_STATE_TABLE")
        if explicit:
            return explicit
        stack_name = (stack or os.getenv("GATEWAY_STACK_NAME") or _DEFAULT_STACK).strip()
        return f"{stack_name}{_TABLE_SUFFIX}"

    def _build_table(self) -> Any:
        """
        Build the ``boto3`` DynamoDB ``Table`` resource.

        ``boto3`` is imported here (not at module import time) so that this
        module can be imported in environments without ``boto3`` or AWS
        credentials.
        """
        try:
            import boto3  # noqa: PLC0415 - intentional lazy import
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "The 'aws' storage backend requires the 'boto3' package. "
                "Install it (see requirements.txt) or use STORAGE_BACKEND=local."
            ) from exc

        resource_kwargs: Dict[str, Any] = {}
        if self._region_name:
            resource_kwargs["region_name"] = self._region_name
        if self._endpoint_url:
            resource_kwargs["endpoint_url"] = self._endpoint_url

        resource = boto3.resource("dynamodb", **resource_kwargs)
        logger.debug(f"Initialized DynamoDB resource for table '{self._table_name}'")
        return resource.Table(self._table_name)

    async def _get_table(self) -> Any:
        """Return the (lazily created) DynamoDB ``Table`` resource."""
        if self._table is not None:
            return self._table
        async with self._init_lock:
            if self._table is None:
                self._table = await asyncio.to_thread(self._build_table)
        return self._table

    # ------------------------------------------------------------------
    # StateStore protocol
    # ------------------------------------------------------------------

    async def get_account_state(self, account_id: str) -> AccountState:
        """
        Return the current shared state for ``account_id``.

        Performs a single ``GetItem``. Returns an empty :class:`AccountState`
        when the account has no item yet (matching the local backend, which
        lazily creates empty state).
        """
        table = await self._get_table()
        response = await asyncio.to_thread(
            lambda: table.get_item(Key=_account_state_key(account_id))
        )
        item = response.get("Item")
        return self._item_to_account_state(account_id, item)

    async def increment_failure(self, account_id: str, failure_time: float) -> AccountState:
        """
        Atomically increment the failure counter and record the failure time.

        Uses ``SET last_failure_time = :t ADD failures :one`` so concurrent
        increments from multiple instances accumulate without lost updates
        (Requirements 7.6). Returns the post-update state (``ALL_NEW``).
        """
        table = await self._get_table()
        response = await asyncio.to_thread(
            lambda: table.update_item(
                Key=_account_state_key(account_id),
                UpdateExpression="SET #lft = :t ADD #f :one",
                ExpressionAttributeNames={"#lft": "last_failure_time", "#f": "failures"},
                ExpressionAttributeValues={":t": Decimal(str(failure_time)), ":one": 1},
                ReturnValues="ALL_NEW",
            )
        )
        attributes = response.get("Attributes")
        return self._item_to_account_state(account_id, attributes)

    async def reset_failure(self, account_id: str) -> None:
        """Reset the failure counter for ``account_id`` to zero (success path)."""
        table = await self._get_table()
        await asyncio.to_thread(
            lambda: table.update_item(
                Key=_account_state_key(account_id),
                UpdateExpression="SET #f = :zero",
                ExpressionAttributeNames={"#f": "failures"},
                ExpressionAttributeValues={":zero": 0},
            )
        )

    async def incr_stats(self, account_id: str, *, total: int, ok: int, failed: int) -> None:
        """
        Atomically increment the usage statistics counters.

        Uses a single ``ADD`` over the three counters so concurrent updates from
        multiple instances accumulate correctly.
        """
        table = await self._get_table()
        await asyncio.to_thread(
            lambda: table.update_item(
                Key=_account_state_key(account_id),
                UpdateExpression="ADD #tr :t, #sr :o, #fr :f",
                ExpressionAttributeNames={
                    "#tr": "total_requests",
                    "#sr": "successful_requests",
                    "#fr": "failed_requests",
                },
                ExpressionAttributeValues={
                    ":t": int(total),
                    ":o": int(ok),
                    ":f": int(failed),
                },
            )
        )

    async def get_sticky_index(self) -> int:
        """Return the global sticky account index (``0`` if unset)."""
        table = await self._get_table()
        response = await asyncio.to_thread(lambda: table.get_item(Key=_sticky_key()))
        item = response.get("Item") or {}
        return _as_int(item.get("sticky_index"), 0)

    async def set_sticky_index(self, index: int) -> None:
        """Set the global sticky account index (last-writer-wins)."""
        table = await self._get_table()
        await asyncio.to_thread(
            lambda: table.update_item(
                Key=_sticky_key(),
                UpdateExpression="SET #si = :idx",
                ExpressionAttributeNames={"#si": "sticky_index"},
                ExpressionAttributeValues={":idx": int(index)},
            )
        )

    async def get_model_mapping(self, model: str) -> List[str]:
        """Return the list of account IDs known to serve ``model``."""
        table = await self._get_table()
        response = await asyncio.to_thread(lambda: table.get_item(Key=_model_key(model)))
        item = response.get("Item") or {}
        accounts = item.get("accounts")
        if not accounts:
            return []
        # DynamoDB String Sets are unordered; return a stable, sorted list.
        return sorted(str(account_id) for account_id in accounts)

    async def add_model_account(self, model: str, account_id: str) -> None:
        """
        Idempotently add ``account_id`` to the account list for ``model``.

        Uses ``ADD accounts :set`` with a single-element DynamoDB **String Set**,
        which de-duplicates server-side, so repeated/concurrent calls are
        idempotent.
        """
        table = await self._get_table()
        await asyncio.to_thread(
            lambda: table.update_item(
                Key=_model_key(model),
                UpdateExpression="ADD #acc :acct",
                ExpressionAttributeNames={"#acc": "accounts"},
                ExpressionAttributeValues={":acct": {account_id}},
            )
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _item_to_account_state(
        account_id: str, item: Optional[Dict[str, Any]]
    ) -> AccountState:
        """Build an :class:`AccountState` from a raw DynamoDB item (or empty)."""
        if not item:
            return AccountState(account_id=account_id)
        return AccountState(
            account_id=account_id,
            failures=_as_int(item.get("failures"), 0),
            last_failure_time=_as_float(item.get("last_failure_time"), 0.0),
            models_cached_at=_as_float(item.get("models_cached_at"), 0.0),
            stats=AccountStatsState(
                total_requests=_as_int(item.get("total_requests"), 0),
                successful_requests=_as_int(item.get("successful_requests"), 0),
                failed_requests=_as_int(item.get("failed_requests"), 0),
            ),
        )
