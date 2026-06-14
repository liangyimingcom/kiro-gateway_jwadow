# -*- coding: utf-8 -*-

"""
Property-based test for design.md Property 10
(熔断器与故障转移状态机行为等价 / circuit-breaker & failover state-machine parity).

This drives the DynamoDB-backed shared-state implementation
(:class:`kiro.backends.aws.state_store.DynamoStateStore`) with the *exact* same
sequence of ``StateStore`` operations that :class:`kiro.account_manager.AccountManager`
performs for each kind of upstream outcome (success / recoverable failure /
fatal failure / INVALID_MODEL_ID), and compares its visible state, step by step,
against a single-host in-memory reference model
(:class:`kiro.backends.local.state_store.LocalStateStore`).

The compared "visible state" is everything the circuit-breaker / failover logic
reads back when it makes a decision:

  * per-account failure counter (``failures``) and last-failure timestamp,
  * per-account usage statistics,
  * the global sticky index,
  * the *derived* decisions: whether each account is in cooldown (the pure
    Property-8 backoff predicate, evaluated at several probe times) and the
    deterministic failover visiting order starting from the sticky index.

Because ``AccountManager``'s control flow never depends on the *return value* of
the failure/stat writes (only on values read back via ``get_account_state`` /
``get_sticky_index``), equivalence of the read-back state implies equivalence of
every failover/circuit-breaker decision. Hence comparing the two stores after an
identical, AccountManager-faithful operation sequence validates the property.

AWS isolation: the real ``DynamoStateStore`` runs against a moto-mocked DynamoDB
table (``from moto import mock_aws``); a fresh table is created per generated
example so examples never contaminate each other.
"""

import asyncio
import itertools

import boto3
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from moto import mock_aws

from kiro.backends.aws.state_store import DynamoStateStore
from kiro.backends.local.state_store import LocalStateStore
from kiro.config import ACCOUNT_MAX_BACKOFF_MULTIPLIER, ACCOUNT_RECOVERY_TIMEOUT


# ==================================================================================================
# Pure derived-decision helpers (mirror AccountManager.get_next_account semantics)
# ==================================================================================================


def _is_in_cooldown(failures: int, last_failure_time: float, now: float) -> bool:
    """
    Deterministic circuit-breaker cooldown predicate (design.md Property 8).

    Mirrors the exponential-backoff gate inside ``AccountManager.get_next_account``:
    an account with ``failures > 0`` is in cooldown while
    ``now - last_failure_time < ACCOUNT_RECOVERY_TIMEOUT * min(2**(failures-1), MAX)``.
    """
    if failures <= 0:
        return False
    backoff_multiplier = min(2 ** (failures - 1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)
    effective_timeout = ACCOUNT_RECOVERY_TIMEOUT * backoff_multiplier
    return (now - last_failure_time) < effective_timeout


def _failover_order(sticky_index: int, account_ids):
    """The deterministic account visiting order starting from the sticky index."""
    n = len(account_ids)
    return [account_ids[(sticky_index + i) % n] for i in range(n)]


# ==================================================================================================
# AccountManager-faithful operation application
# ==================================================================================================


class _ManagerMirror:
    """
    Tracks the minimal application-level state AccountManager keeps in memory and
    uses to *decide which* StateStore operations to issue (the ``reset_failure``
    guard and the sticky-index change guard). Stats / last-failure-time do not
    gate any operation, so they are not mirrored here.
    """

    def __init__(self, account_ids):
        self.failures = {aid: 0 for aid in account_ids}
        self.sticky = 0  # global sticky *index*


async def _apply_event(store, mirror, account_ids, kind: str, idx: int, now: float) -> None:
    """
    Apply one upstream outcome to ``store`` exactly as AccountManager would.

    * ``success``     -> ``report_success``: reset failures (guarded), incr stats
                         (total+ok), move sticky to this account (guarded).
    * ``recoverable`` -> ``report_failure(RECOVERABLE)``: increment_failure, incr
                         stats (total+failed). Sticky unchanged.
    * ``fatal``       -> ``report_failure(FATAL)``: incr stats (total+failed) only;
                         failures unchanged. Sticky unchanged.
    * ``invalid``     -> ``report_failure(INVALID_MODEL_ID)``: incr stats (total
                         only); failures unchanged. Sticky unchanged.
    """
    aid = account_ids[idx]

    if kind == "success":
        if mirror.failures[aid] > 0:
            mirror.failures[aid] = 0
            await store.reset_failure(aid)
        await store.incr_stats(aid, total=1, ok=1, failed=0)
        if mirror.sticky != idx:
            mirror.sticky = idx
            await store.set_sticky_index(idx)
    elif kind == "recoverable":
        mirror.failures[aid] += 1
        await store.increment_failure(aid, now)
        await store.incr_stats(aid, total=1, ok=0, failed=1)
    elif kind == "fatal":
        await store.incr_stats(aid, total=1, ok=0, failed=1)
    elif kind == "invalid":
        await store.incr_stats(aid, total=1, ok=0, failed=0)
    else:  # pragma: no cover - defensive
        raise AssertionError(f"unknown event kind: {kind!r}")


async def _assert_visible_state_equiv(ref, sub, account_ids, now: float) -> None:
    """Assert the DynamoDB store and the in-memory reference expose identical visible state."""
    for aid in account_ids:
        r = await ref.get_account_state(aid)
        s = await sub.get_account_state(aid)

        assert r.failures == s.failures, (aid, "failures", r.failures, s.failures)
        assert r.last_failure_time == s.last_failure_time, (
            aid,
            "last_failure_time",
            r.last_failure_time,
            s.last_failure_time,
        )
        assert r.stats.total_requests == s.stats.total_requests, (aid, "total")
        assert r.stats.successful_requests == s.stats.successful_requests, (aid, "ok")
        assert r.stats.failed_requests == s.stats.failed_requests, (aid, "failed")

        # Derived skip decision must agree.
        assert _is_in_cooldown(r.failures, r.last_failure_time, now) == _is_in_cooldown(
            s.failures, s.last_failure_time, now
        ), (aid, "cooldown")

    ref_sticky = await ref.get_sticky_index()
    sub_sticky = await sub.get_sticky_index()
    assert ref_sticky == sub_sticky, ("sticky", ref_sticky, sub_sticky)
    assert _failover_order(ref_sticky, account_ids) == _failover_order(sub_sticky, account_ids)


async def _assert_cooldown_equiv_at(ref, sub, account_ids, probe_now: float) -> None:
    """Assert the skip (cooldown) decision agrees across stores at an arbitrary probe time."""
    for aid in account_ids:
        r = await ref.get_account_state(aid)
        s = await sub.get_account_state(aid)
        assert _is_in_cooldown(r.failures, r.last_failure_time, probe_now) == _is_in_cooldown(
            s.failures, s.last_failure_time, probe_now
        ), (aid, "cooldown@probe", probe_now)


async def _run_example(table, account_ids, events) -> None:
    """Drive both stores through ``events`` and assert step-by-step equivalence."""
    sub = DynamoStateStore(table=table)  # DynamoDB-backed implementation under test
    ref = LocalStateStore("__property10_unused_state__.json")  # single-host in-memory reference
    sub_mirror = _ManagerMirror(account_ids)
    ref_mirror = _ManagerMirror(account_ids)

    base = 1000.0
    for step, (kind, idx) in enumerate(events):
        now = base + float(step + 1)  # strictly increasing integer-valued logical clock
        await _apply_event(sub, sub_mirror, account_ids, kind, idx, now)
        await _apply_event(ref, ref_mirror, account_ids, kind, idx, now)
        await _assert_visible_state_equiv(ref, sub, account_ids, now)

    # Cross-time cooldown probes: "just after the last event" (likely in cooldown)
    # and "far in the future" (half-open). Both must agree across stores.
    final_now = base + float(len(events) + 1)
    await _assert_cooldown_equiv_at(ref, sub, account_ids, final_now)
    await _assert_cooldown_equiv_at(ref, sub, account_ids, final_now + 10 ** 9)


# ==================================================================================================
# Strategy
# ==================================================================================================

_EVENT_KINDS = st.sampled_from(["success", "recoverable", "fatal", "invalid"])


@st.composite
def _scenarios(draw):
    """A pool of accounts plus an arbitrary event sequence over those accounts."""
    num_accounts = draw(st.integers(min_value=1, max_value=5))
    events = draw(
        st.lists(
            st.tuples(_EVENT_KINDS, st.integers(min_value=0, max_value=num_accounts - 1)),
            min_size=0,
            max_size=30,
        )
    )
    return num_accounts, events


# ==================================================================================================
# Property test
# ==================================================================================================


# Feature: aws-cloud-native-gateway, Property 10: 对任意由成功/可恢复失败/致命失败/INVALID_MODEL_ID 组成的事件序列，以单机内存 AccountManager 为参考模型驱动 DynamoDB 实现后，二者每一步可见状态等价
# Validates: Requirements 7.7
def test_property_10_statemachine_behavioural_parity():
    """
    For any event sequence built from success / recoverable failure / fatal
    failure / INVALID_MODEL_ID, the DynamoDB-backed StateStore exposes the same
    visible state (failure counts, skip/cooldown decisions, sticky index and
    failover order) at every step as the single-host in-memory reference model.

    Validates: Requirements 7.7
    """
    table_counter = itertools.count()

    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        @given(_scenarios())
        @settings(
            max_examples=150,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow],
        )
        def _check(scenario):
            num_accounts, events = scenario
            account_ids = [f"acc{i}" for i in range(num_accounts)]
            table = dynamodb.create_table(
                TableName=f"kiro-gateway-state-{next(table_counter)}",
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            asyncio.run(_run_example(table, account_ids, events))

        _check()
