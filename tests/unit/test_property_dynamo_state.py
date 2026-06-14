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
Property-based tests for :class:`kiro.backends.aws.state_store.DynamoStateStore`.

Covers two of the design.md "正确性属性" against a moto-mocked DynamoDB service
(``from moto import mock_aws``). DynamoDB is the *external* dependency being
mocked; the ``DynamoStateStore`` logic under test (atomic ``ADD`` failure
increments, ``GetItem`` reads) is the real implementation.

- Task 7.2 -> Property 9 (失败计数原子性 / failure-count atomicity, no lost
  updates) - Validates Requirements 7.6.
- Task 7.3 -> Property 7 (共享状态跨实例可见 / shared state visible across
  instances) - Validates Requirements 7.3, 7.5.

Each property runs >= 100 random iterations (Hypothesis ``max_examples=100``).
Each Hypothesis example gets a *fresh* moto DynamoDB table created with the same
PK/SK single-table schema used by ``DynamoStateStore`` (PK String HASH, SK String
RANGE), and a real ``DynamoStateStore`` is injected with that table.

Concurrency note (moto thread-safety):
    moto's in-memory DynamoDB backend is **not thread-safe** - when several
    operations are issued from worker threads at the same time (as Property 9
    requires via ``asyncio.gather`` + ``asyncio.to_thread``), moto can interleave
    a single ``UpdateItem``'s read-modify-write and *lose updates* that the real
    DynamoDB service would never lose (real single-item writes are atomic and
    isolated). To keep moto a faithful stand-in for the real service, every boto3
    call is routed through :class:`_ThreadSafeProxy`, a thin wrapper that
    serialises each operation under a shared lock so that each DynamoDB operation
    is atomic/isolated - exactly the per-item guarantee DynamoDB provides. The
    implementation is still exercised under genuine concurrency (the calls are
    still launched together across threads); only moto's missing isolation is
    restored. An implementation that used a non-atomic read-modify-write across
    *two* calls (e.g. GetItem then PutItem) would still lose updates under this
    harness, so the atomicity property is tested faithfully.
"""

import asyncio
import threading
from decimal import Decimal

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from kiro.backends.aws.state_store import (  # noqa: E402
    PK_ATTR,
    SK_ATTR,
    DynamoStateStore,
    _account_state_key,
)

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

REGION = "us-east-1"
TABLE_NAME = "kiro-gateway-state"

# Account ids restricted to characters that are unambiguous inside the single
# table's composite keys (``ACCT#<id>``) and valid DynamoDB string keys.
_ACCOUNT_ID = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=16,
)

# Realistic ``failure_time`` values (epoch seconds). Constrained to DynamoDB's
# supported number domain: the service rejects magnitudes below ~1e-130, and a
# real failure timestamp is either 0.0 or a positive epoch-second value, so we
# generate 0.0 or a finite value in [1.0, 2e9] (no NaN/Inf/subnormals). This
# keeps the generator inside the system's actual input space.
_FAILURE_TIME = st.one_of(
    st.just(0.0),
    st.floats(
        min_value=1.0,
        max_value=2_000_000_000.0,
        allow_nan=False,
        allow_infinity=False,
        allow_subnormal=False,
    ),
)


class _ThreadSafeProxy:
    """
    Serialise every method call to a wrapped boto3 object through a shared lock.

    Compensates for moto's lack of thread-safety so that each DynamoDB operation
    runs atomically and in isolation, matching the real service's per-item
    guarantee. Non-callable attributes are passed through unchanged.
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


def _create_state_table():
    """
    Create a fresh DynamoDB table matching the ``DynamoStateStore`` single-table
    schema (PK String HASH, SK String RANGE) and return the boto3 resource Table.

    Must be called inside an active ``mock_aws()`` context.
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
    return table


# ==================================================================================================
# Task 7.2 -> Property 9: 失败计数原子性（无丢失更新）
# ==================================================================================================


# Feature: aws-cloud-native-gateway, Property 9: 对任意初始失败计数与任意 N 次并发 increment_failure 调用，最终 failures 值等于初始值加 N
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    account_id=_ACCOUNT_ID,
    initial=st.integers(min_value=0, max_value=50),
    n=st.integers(min_value=1, max_value=20),
    failure_time=_FAILURE_TIME,
)
def test_property_9_failure_count_atomic_no_lost_updates(account_id, initial, n, failure_time):
    """
    Property 9 (失败计数原子性 / atomicity): for any initial failure count and any
    number N of *concurrent* ``increment_failure`` calls, the final ``failures``
    value equals ``initial + N`` - the server-side atomic ``ADD`` never loses an
    update under concurrency.

    **Validates: Requirements 7.6**
    """
    with mock_aws():
        table = _create_state_table()
        # Seed the account with the chosen initial failure count.
        if initial:
            table.put_item(
                Item={**_account_state_key(account_id), "failures": Decimal(initial)}
            )

        # Single DynamoDB table shared by all concurrent operations, made
        # atomic/isolated per-operation via the thread-safe proxy.
        shared = _ThreadSafeProxy(table, threading.Lock())

        async def _drive():
            # Construct the store inside the running loop so its internal
            # asyncio.Lock binds to the active loop (Python 3.9 binds at __init__).
            store = DynamoStateStore(table=shared)
            # Fire N increments concurrently (each dispatched to a worker thread
            # by the store via asyncio.to_thread) and gather them together.
            await asyncio.gather(
                *(store.increment_failure(account_id, failure_time) for _ in range(n))
            )
            return await store.get_account_state(account_id)

        final_state = asyncio.run(_drive())

    assert final_state.failures == initial + n


# ==================================================================================================
# Task 7.3 -> Property 7: 共享状态跨实例可见
# ==================================================================================================


# Feature: aws-cloud-native-gateway, Property 7: 对任意账号与任意失败/成功事件序列，一个实例写入后另一实例在缓存过期后读取应观察到一致状态
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    account_id=_ACCOUNT_ID,
    events=st.lists(st.tuples(st.booleans(), _FAILURE_TIME), max_size=30),
)
def test_property_7_shared_state_visible_across_instances(account_id, events):
    """
    Property 7 (共享状态跨实例可见 / cross-instance visibility): for any account
    and any sequence of failure/success events, after one store instance writes
    the events, a *separate* store instance pointed at the same table (its own
    read path, no stale cache) observes a consistent account state matching a
    reference model.

    ``events`` is a list of ``(is_failure, failure_time)``:
      - ``is_failure=True``  -> ``increment_failure`` (failures += 1, records time)
      - ``is_failure=False`` -> ``reset_failure``     (failures = 0, success path)

    **Validates: Requirements 7.3, 7.5**
    """
    # Reference model of the expected post-sequence state.
    expected_failures = 0
    expected_last_failure_time = 0.0
    for is_failure, t in events:
        if is_failure:
            expected_failures += 1
            expected_last_failure_time = t
        else:
            expected_failures = 0  # reset_failure leaves last_failure_time untouched

    with mock_aws():
        table = _create_state_table()
        # One shared DynamoDB table behind two independent store instances -
        # modelling two Gateway instances in the Fargate pool.
        shared = _ThreadSafeProxy(table, threading.Lock())

        async def _drive():
            # Construct the stores inside the running loop so their internal
            # asyncio.Lock binds to the active loop (Python 3.9 binds at __init__).
            writer = DynamoStateStore(table=shared)
            reader = DynamoStateStore(table=shared)
            for is_failure, t in events:
                if is_failure:
                    await writer.increment_failure(account_id, t)
                else:
                    await writer.reset_failure(account_id)
            # The reader is a distinct instance and must observe what the writer
            # persisted (cross-instance visibility, no per-instance cache reused).
            reader_state = await reader.get_account_state(account_id)
            writer_state = await writer.get_account_state(account_id)
            return reader_state, writer_state

        reader_state, writer_state = asyncio.run(_drive())

    # The second instance observes a state consistent with the reference model...
    assert reader_state.failures == expected_failures
    assert reader_state.last_failure_time == pytest.approx(expected_last_failure_time)
    # ...and identical to what the writing instance reads back (one shared store).
    assert reader_state == writer_state
