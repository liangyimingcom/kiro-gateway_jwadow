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
Property-based test for :class:`kiro.backends.aws.coordinator.DynamoRefreshCoordinator`.

Covers the design.md "正确性属性" 属性 6 (令牌刷新单飞 / single-flight token
refresh) against a moto-mocked DynamoDB service (``from moto import mock_aws``).
DynamoDB is the *external* dependency being mocked; the lease-lock logic under
test (conditional ``UpdateItem`` with ``attribute_not_exists(lock_owner) OR
lock_expires_at < :now``) is the real implementation.

- Task 7.5 -> Property 6 - Validates Requirements 6.7, 7.7.

Each property runs >= 100 random iterations (Hypothesis ``max_examples=100``).
Every example gets a *fresh* moto DynamoDB table created with the same PK/SK
single-table schema used by the coordinator (PK String HASH, SK String RANGE),
shared by a set of independent coordinator instances (distinct ``instance_id``)
that all race to refresh the *same* account's token concurrently.

Concurrency note (moto thread-safety):
    moto's in-memory DynamoDB backend is **not thread-safe** - under genuinely
    concurrent requests it can let *two* conditional writes both succeed, electing
    more than one lock winner, which the real DynamoDB service (atomic, isolated
    conditional writes) would never do. To keep moto a faithful stand-in, every
    boto3 call is routed through :class:`_ThreadSafeProxy`, which serialises each
    operation under a shared lock so each conditional ``UpdateItem`` is atomic and
    isolated - exactly DynamoDB's per-item guarantee. The coordinators are still
    raced under genuine concurrency (the acquire calls are launched together
    across threads); only moto's missing isolation is restored, so single-flight
    is tested faithfully.
"""

import asyncio
import threading

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from kiro.backends.aws.coordinator import DynamoRefreshCoordinator  # noqa: E402

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

REGION = "us-east-1"
TABLE_NAME = "kiro-gateway-state"

# PK/SK attribute names (the coordinator uses the same single-table layout as the
# state store: a String partition key "PK" and String sort key "SK").
PK_ATTR = "PK"
SK_ATTR = "SK"

_ACCOUNT_ID = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=16,
)


class _ThreadSafeProxy:
    """
    Serialise every method call to a wrapped boto3 object through a shared lock.

    Compensates for moto's lack of thread-safety so that each DynamoDB operation
    (here, the conditional ``UpdateItem`` that implements the lease lock) runs
    atomically and in isolation, matching the real service's per-item guarantee.
    Non-callable attributes are passed through unchanged.
    """

    def __init__(self, wrapped, lock):
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_lock", lock)

    def __getattr__(self, name):
        attr = getattr(self._wrapped, name)
        if callable(attr):
            lock = self._lock

            def _locked(*args, **kwargs):
                with lock:
                    return attr(*args, **kwargs)

            return _locked
        return attr


def _create_table():
    """
    Create a fresh DynamoDB single table (PK String HASH, SK String RANGE) and
    return its name. Must be called inside an active ``mock_aws()`` context.
    """
    resource = boto3.resource("dynamodb", region_name=REGION)
    table = resource.create_table(
        TableName=TABLE_NAME,
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
    return TABLE_NAME


# ==================================================================================================
# Task 7.5 -> Property 6: 令牌刷新单飞（无重复刷新）
# ==================================================================================================


# Feature: aws-cloud-native-gateway, Property 6: 对任意账号与任意数量并发刷新同一账号的实例集合，在一次刷新窗口内对上游刷新端点的实际刷新调用次数恰为 1
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    account_id=_ACCOUNT_ID,
    num_instances=st.integers(min_value=1, max_value=15),
    lease_ttl=st.integers(min_value=30, max_value=3600),
)
def test_property_6_token_refresh_single_flight(account_id, num_instances, lease_ttl):
    """
    Property 6 (令牌刷新单飞 / single-flight): for any account and any number of
    instances concurrently attempting to refresh the *same* account's token
    within one refresh window, exactly ONE instance actually performs the
    upstream refresh; the others must reuse the result.

    We model "actually performs the upstream refresh" as "won the lease lock":
    each independent ``DynamoRefreshCoordinator`` (distinct ``instance_id``) races
    to ``acquire_lock`` on a fresh lock with a lease TTL long enough that it does
    not expire during the race. The number of winners must be exactly 1.

    **Validates: Requirements 6.7, 7.7**
    """
    with mock_aws():
        _create_table()
        # A single shared low-level DynamoDB client backs all coordinator
        # instances, mirroring one DynamoDB table shared by the whole Fargate
        # pool. The thread-safe proxy makes each conditional write atomic/isolated
        # (restoring the isolation moto lacks under concurrency).
        client = _ThreadSafeProxy(
            boto3.client("dynamodb", region_name=REGION), threading.Lock()
        )
        coordinators = [
            DynamoRefreshCoordinator(
                table_name=TABLE_NAME,
                dynamodb_client=client,
                instance_id=f"instance-{i}",
            )
            for i in range(num_instances)
        ]

        async def _race():
            # All instances attempt to seize the refresh lease at once; the
            # number that report success is the number of upstream refreshes.
            results = await asyncio.gather(
                *(coord.acquire_lock(account_id, lease_ttl) for coord in coordinators)
            )
            return sum(1 for won in results if won)

        upstream_refreshes = asyncio.run(_race())

    assert upstream_refreshes == 1
