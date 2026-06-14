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
DynamoRefreshCoordinator - distributed single-flight token refresh coordinator
backed by DynamoDB (cloud-native form).

In a multi-instance Fargate pool several instances may notice that the same
account's token is about to expire at almost the same time. To avoid every
instance hammering the upstream OIDC refresh endpoint, refresh is made
*single-flight*: exactly one instance acquires a short-lived **lease lock** via a
DynamoDB conditional write, performs the refresh, persists the new token to the
Secret_Store (Secrets Manager) plus token metadata to DynamoDB, then releases the
lock. The instances that fail to acquire the lock poll the token metadata with a
bounded backoff and reuse the refreshed token instead of refreshing themselves
(Requirements 6.7, 7.7).

See design.md "令牌刷新（带分布式锁）流" for the sequence diagram and the lock /
token item layout.

Item layout (single-table design):

- Lease lock:
    PK = ``LOCK#<account_id>``, SK = ``LEASE``
    attributes: ``lock_owner`` (this instance id), ``lock_expires_at`` (epoch
    seconds), ``expires_at`` (DynamoDB TTL attribute, epoch seconds).

- Token metadata:
    PK = ``TOKEN#<account_id>``, SK = ``META``
    attributes: ``token_expires_at`` (epoch seconds or absent), ``updated_at``
    (epoch seconds, set every time a fresh token is published), ``profile_arn``,
    ``region``. The sensitive token material itself lives in the Secret_Store.

The AWS clients are created lazily so this module can be imported (and the rest
of the abstraction layer can be exercised) without any AWS credentials present.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from typing import Optional

from loguru import logger

from kiro.backends.interfaces import TokenBundle

# Default DynamoDB single-table name (overridable via env / constructor).
_DEFAULT_TABLE_ENV = "DYNAMODB_TABLE_NAME"
# Region resolution falls back through these environment variables.
_REGION_ENVS = ("AWS_REGION", "AWS_DEFAULT_REGION", "KIRO_REGION")

# Sort-key constants for the single-table items.
_LOCK_SK = "LEASE"
_TOKEN_SK = "META"

# Bounded-backoff polling parameters for :meth:`wait_for_token`.
_POLL_INITIAL_DELAY = 0.1
_POLL_MAX_DELAY = 1.0
_POLL_BACKOFF_FACTOR = 2.0


def _lock_pk(account_id: str) -> str:
    """Return the partition key for the lease lock of ``account_id``."""
    return f"LOCK#{account_id}"


def _token_pk(account_id: str) -> str:
    """Return the partition key for the token metadata of ``account_id``."""
    return f"TOKEN#{account_id}"


def _bundle_to_json(token: TokenBundle) -> str:
    """Serialise a :class:`TokenBundle` to a JSON string for the Secret_Store."""
    return json.dumps(
        {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
            "profile_arn": token.profile_arn,
            "region": token.region,
        }
    )


def _coerce_epoch(value) -> Optional[float]:
    """
    Coerce a persisted ``expires_at`` value into epoch seconds.

    Accepts numbers (already epoch seconds) and ISO-8601 strings (as written by
    ``KiroAuthManager._persist_refreshed_token``) so that a token persisted by
    either path round-trips correctly.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None
    return None


def _json_to_bundle(raw: str) -> TokenBundle:
    """Parse a JSON token payload from the Secret_Store into a :class:`TokenBundle`."""
    data = json.loads(raw)
    return TokenBundle(
        access_token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        expires_at=_coerce_epoch(data.get("expires_at")),
        profile_arn=data.get("profile_arn"),
        region=data.get("region"),
    )


class DynamoRefreshCoordinator:
    """
    Distributed single-flight token refresh coordinator backed by DynamoDB.

    Implements the :class:`~kiro.backends.interfaces.TokenRefreshCoordinator`
    protocol. A short-lived lease lock (conditional write) elects a single
    refresher per account; refreshed tokens are persisted centrally (Secrets
    Manager + DynamoDB metadata) so that other instances reuse them rather than
    issuing duplicate upstream refreshes.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        *,
        secret_provider=None,
        region: Optional[str] = None,
        instance_id: Optional[str] = None,
        dynamodb_client=None,
        token_secret_prefix: str = "",
    ) -> None:
        """
        Args:
            table_name: DynamoDB single-table name. Defaults to the
                ``DYNAMODB_TABLE_NAME`` environment variable.
            secret_provider: Optional :class:`SecretProvider` used to persist the
                sensitive token material centrally (Secrets Manager). When
                ``None``, only the DynamoDB token metadata is written and
                :meth:`wait_for_token` can only return metadata (no access token).
            region: AWS region for the DynamoDB client. Resolved from the
                environment when ``None``.
            instance_id: Stable identifier for this gateway instance, used as the
                lease ``lock_owner``. A unique value is generated when ``None``.
            dynamodb_client: Pre-built boto3 DynamoDB client (mainly for tests /
                dependency injection). When ``None`` the client is created lazily
                on first use so importing this module never requires AWS creds.
            token_secret_prefix: Optional prefix prepended to ``account_id`` when
                deriving the Secret_Store name for a token.
        """
        self._table_name = table_name or os.getenv(_DEFAULT_TABLE_ENV, "")
        self._secret_provider = secret_provider
        self._region = region or self._resolve_region()
        self._instance_id = instance_id or f"{socket.gethostname()}-{uuid.uuid4().hex}"
        self._token_secret_prefix = token_secret_prefix
        self._client = dynamodb_client

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_region() -> Optional[str]:
        """Resolve the AWS region from the supported environment variables."""
        for env in _REGION_ENVS:
            value = os.getenv(env)
            if value:
                return value
        return None

    def _get_client(self):
        """Lazily build (and cache) the boto3 DynamoDB client."""
        if self._client is None:
            import boto3  # Imported lazily so module import needs no AWS creds.

            kwargs = {}
            if self._region:
                kwargs["region_name"] = self._region
            self._client = boto3.client("dynamodb", **kwargs)
        return self._client

    def _token_secret_name(self, account_id: str) -> str:
        """Return the Secret_Store name used to persist ``account_id``'s token."""
        return f"{self._token_secret_prefix}{account_id}"

    @staticmethod
    def _is_conditional_check_failed(exc: Exception) -> bool:
        """Return ``True`` if ``exc`` is a DynamoDB ConditionalCheckFailedException."""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code")
            return code == "ConditionalCheckFailedException"
        return exc.__class__.__name__ == "ConditionalCheckFailedException"

    # ------------------------------------------------------------------
    # TokenRefreshCoordinator protocol
    # ------------------------------------------------------------------

    async def acquire_lock(self, account_id: str, ttl_seconds: int) -> bool:
        """
        Attempt to acquire the refresh lease lock for ``account_id``.

        Performs a DynamoDB conditional ``UpdateItem`` that succeeds only when no
        live lease exists (``attribute_not_exists(lock_owner)``) or the existing
        lease has expired (``lock_expires_at < now``). On success this instance
        becomes the lock owner with a fresh ``ttl_seconds`` lease.

        Returns:
            ``True`` if the lease was acquired, ``False`` if another instance
            currently holds a live lease (ConditionalCheckFailed).
        """
        now = time.time()
        expires_at = now + max(0, ttl_seconds)

        def _call():
            return self._get_client().update_item(
                TableName=self._table_name,
                Key={"PK": {"S": _lock_pk(account_id)}, "SK": {"S": _LOCK_SK}},
                UpdateExpression=(
                    "SET lock_owner = :owner, lock_expires_at = :exp, "
                    "expires_at = :ttl"
                ),
                ConditionExpression=(
                    "attribute_not_exists(lock_owner) OR lock_expires_at < :now"
                ),
                ExpressionAttributeValues={
                    ":owner": {"S": self._instance_id},
                    ":exp": {"N": repr(expires_at)},
                    ":ttl": {"N": repr(expires_at)},
                    ":now": {"N": repr(now)},
                },
            )

        try:
            await asyncio.to_thread(_call)
            return True
        except Exception as exc:  # noqa: BLE001 - inspect for conditional failure
            if self._is_conditional_check_failed(exc):
                return False
            logger.error(f"DynamoRefreshCoordinator.acquire_lock failed for {account_id}: {exc}")
            raise

    async def release_lock(self, account_id: str) -> None:
        """
        Release the refresh lease lock for ``account_id``.

        The lease is deleted only when still owned by this instance
        (``lock_owner = this``); a lease that has already expired and been taken
        over by another instance is left untouched. A lost-ownership condition is
        treated as a successful (idempotent) release.
        """
        def _call():
            return self._get_client().delete_item(
                TableName=self._table_name,
                Key={"PK": {"S": _lock_pk(account_id)}, "SK": {"S": _LOCK_SK}},
                ConditionExpression="lock_owner = :owner",
                ExpressionAttributeValues={":owner": {"S": self._instance_id}},
            )

        try:
            await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001 - inspect for conditional failure
            if self._is_conditional_check_failed(exc):
                # We no longer own the lease (expired / taken over). Nothing to do.
                return
            logger.error(f"DynamoRefreshCoordinator.release_lock failed for {account_id}: {exc}")
            raise

    async def store_refreshed_token(self, account_id: str, token: TokenBundle) -> None:
        """
        Persist a refreshed token so other instances can reuse it.

        Writes the sensitive token material to the Secret_Store (via the injected
        :class:`SecretProvider`, when present) and publishes non-sensitive token
        metadata (expiry, profile ARN, region and an ``updated_at`` marker) to the
        DynamoDB ``TOKEN#<account_id>`` / ``META`` item (Requirements 6.7).
        """
        if self._secret_provider is not None:
            try:
                await self._secret_provider.put_secret(
                    self._token_secret_name(account_id), _bundle_to_json(token)
                )
            except Exception as exc:  # noqa: BLE001 - defensive, log and continue
                logger.error(
                    f"DynamoRefreshCoordinator failed to persist token secret for {account_id}: {exc}"
                )

        now = time.time()
        item = {
            "PK": {"S": _token_pk(account_id)},
            "SK": {"S": _TOKEN_SK},
            "updated_at": {"N": repr(now)},
        }
        if token.expires_at is not None:
            item["token_expires_at"] = {"N": repr(float(token.expires_at))}
        if token.profile_arn:
            item["profile_arn"] = {"S": token.profile_arn}
        if token.region:
            item["region"] = {"S": token.region}

        def _call():
            return self._get_client().put_item(TableName=self._table_name, Item=item)

        try:
            await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.error(
                f"DynamoRefreshCoordinator failed to write token metadata for {account_id}: {exc}"
            )

    async def wait_for_token(self, account_id: str, timeout: float) -> Optional[TokenBundle]:
        """
        Wait (bounded by ``timeout`` seconds) for a token refreshed by the lock
        holder, polling the DynamoDB token metadata with exponential backoff.

        A token is considered ready when its ``updated_at`` marker is at least the
        moment this wait began (i.e. it was published by the in-progress refresh,
        not a stale value). When ready, the sensitive token material is read back
        from the Secret_Store and returned as a :class:`TokenBundle`.

        Returns:
            The refreshed :class:`TokenBundle` once available, or ``None`` if the
            timeout elapsed first (the caller may then degrade to a self-refresh).
        """
        deadline = time.monotonic() + max(0.0, timeout)
        baseline = time.time()
        delay = _POLL_INITIAL_DELAY

        while True:
            bundle = await self._read_ready_token(account_id, baseline)
            if bundle is not None:
                return bundle

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * _POLL_BACKOFF_FACTOR, _POLL_MAX_DELAY)

    async def _read_ready_token(self, account_id: str, baseline: float) -> Optional[TokenBundle]:
        """
        Read the token metadata and, if a fresh token (``updated_at >= baseline``)
        is published, fetch and return the full token bundle. Returns ``None`` when
        no fresh token is available yet.
        """
        def _call():
            return self._get_client().get_item(
                TableName=self._table_name,
                Key={"PK": {"S": _token_pk(account_id)}, "SK": {"S": _TOKEN_SK}},
                ConsistentRead=True,
            )

        try:
            response = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001 - defensive, treat as not-ready
            logger.debug(f"DynamoRefreshCoordinator poll failed for {account_id}: {exc}")
            return None

        item = response.get("Item")
        if not item:
            return None

        updated_at_attr = item.get("updated_at", {}).get("N")
        if updated_at_attr is None or float(updated_at_attr) < baseline:
            # No token has been published since we started waiting.
            return None

        return await self._load_token_bundle(account_id, item)

    async def _load_token_bundle(self, account_id: str, item: dict) -> Optional[TokenBundle]:
        """
        Build a :class:`TokenBundle` for a ready token, reading the sensitive
        material from the Secret_Store and falling back to DynamoDB metadata for
        the non-sensitive fields.
        """
        bundle: Optional[TokenBundle] = None
        if self._secret_provider is not None:
            try:
                raw = await self._secret_provider.get_secret(self._token_secret_name(account_id))
                bundle = _json_to_bundle(raw)
            except Exception as exc:  # noqa: BLE001 - fall back to metadata-only
                logger.debug(
                    f"DynamoRefreshCoordinator failed to read token secret for {account_id}: {exc}"
                )

        if bundle is None:
            bundle = TokenBundle()

        # Backfill non-sensitive metadata from DynamoDB when missing.
        if bundle.expires_at is None and "token_expires_at" in item:
            bundle.expires_at = float(item["token_expires_at"]["N"])
        if not bundle.profile_arn and "profile_arn" in item:
            bundle.profile_arn = item["profile_arn"]["S"]
        if not bundle.region and "region" in item:
            bundle.region = item["region"]["S"]

        return bundle
