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
Integration test (Task 11.2): dual-backend equivalence.

Runs the *same* sequence of storage-layer operations against the ``local``
backend and the ``aws`` backend (moto-mocked) and asserts the observable results
are equivalent. This demonstrates the backend abstraction (StateStore /
ConfigProvider / SecretProvider) is backward compatible: swapping
``STORAGE_BACKEND`` from ``local`` to ``aws`` does not change observable
behaviour (design.md "测试策略 → 集成测试 → 双后端等价").

The AWS State_Store read cache is disabled here (``STATE_CACHE_TTL_SECONDS=0``)
so the comparison is against the raw backend semantics rather than a process
cache hit; the cache itself has its own dedicated coverage (Task 11.4).

Requirements: 5.5 (config source swap without behaviour change), 10.1 / 10.2 /
10.5 (backward-compatible contract preserved across the abstraction).
"""

import asyncio
import json
import os

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from kiro.backends.aws.state_store import PK_ATTR, SK_ATTR  # noqa: E402
from kiro.backends.factory import create_backend  # noqa: E402
from kiro.backends.interfaces import MissingConfigError  # noqa: E402

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

REGION = "us-east-1"
STACK = "kiro-dual"
STATE_TABLE = f"{STACK}-gateway-state"
DEBUG_BUCKET = f"{STACK}-gateway-debug"


@pytest.fixture
def dual_aws_env(monkeypatch):
    """
    Configure the AWS-backend environment for the equivalence run and restore it
    afterwards. The state cache is disabled so we compare raw backend semantics.
    ``STORAGE_BACKEND`` is set but ``create_backend`` is always called with an
    explicit name, so it can never leak a surprising default into other tests.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "aws")
    monkeypatch.setenv("STACK_NAME", STACK)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("GATEWAY_STATE_TABLE", STATE_TABLE)
    monkeypatch.setenv("DEBUG_S3_BUCKET", DEBUG_BUCKET)
    monkeypatch.setenv("STATE_CACHE_TTL_SECONDS", "0")
    yield


def _create_state_table():
    """Create the DynamoDB single table (PK/SK schema). Inside ``mock_aws()``."""
    dynamo = boto3.resource("dynamodb", region_name=REGION)
    table = dynamo.create_table(
        TableName=STATE_TABLE,
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


async def _run_state_operations(state_store) -> dict:
    """
    Drive a deterministic sequence of StateStore operations and capture the
    observable results as a backend-agnostic dict.

    Covers: empty-state read, atomic failure increment, failure reset, stats
    accumulation, sticky index round-trip and idempotent model->account mapping.
    """
    obs: dict = {}

    initial = await state_store.get_account_state("a1")
    obs["initial_failures"] = initial.failures
    obs["initial_stats"] = (
        initial.stats.total_requests,
        initial.stats.successful_requests,
        initial.stats.failed_requests,
    )

    s1 = await state_store.increment_failure("a1", 10.0)
    s2 = await state_store.increment_failure("a1", 20.0)
    obs["after_inc1"] = s1.failures
    obs["after_inc2"] = s2.failures
    obs["last_failure_time"] = s2.last_failure_time

    after_two = await state_store.get_account_state("a1")
    obs["read_after_two"] = after_two.failures

    await state_store.reset_failure("a1")
    obs["after_reset"] = (await state_store.get_account_state("a1")).failures

    await state_store.incr_stats("a1", total=3, ok=2, failed=1)
    await state_store.incr_stats("a1", total=3, ok=2, failed=1)
    stats = (await state_store.get_account_state("a1")).stats
    obs["stats"] = (stats.total_requests, stats.successful_requests, stats.failed_requests)

    obs["sticky_default"] = await state_store.get_sticky_index()
    await state_store.set_sticky_index(2)
    obs["sticky_after_set"] = await state_store.get_sticky_index()

    await state_store.add_model_account("m1", "a1")
    await state_store.add_model_account("m1", "a1")  # idempotent
    await state_store.add_model_account("m1", "a2")
    obs["model_mapping"] = sorted(await state_store.get_model_mapping("m1"))
    obs["model_mapping_unknown"] = await state_store.get_model_mapping("no-such-model")

    return obs


@pytest.mark.asyncio
async def test_dual_backend_state_store_equivalence(dual_aws_env):
    """
    The local and AWS State_Store backends produce equivalent observable results
    for an identical operation sequence (backward compatibility).

    Requirements: 5.5, 10.1, 10.2, 10.5
    """
    # --- local backend (in-memory, behaviourally equivalent to current) ---
    local_bundle = create_backend("local")
    local_obs = await _run_state_operations(local_bundle.state_store)

    # --- aws backend (DynamoDB via moto), cache disabled ---
    with mock_aws():
        _create_state_table()
        aws_bundle = create_backend("aws")
        aws_obs = await _run_state_operations(aws_bundle.state_store)

    assert local_obs == aws_obs, (
        f"backend divergence:\n local={local_obs}\n aws  ={aws_obs}"
    )
    # Sanity: the sequence actually exercised meaningful state transitions.
    assert local_obs["after_inc2"] == 2
    assert local_obs["after_reset"] == 0
    assert local_obs["stats"] == (6, 4, 2)
    assert local_obs["sticky_after_set"] == 2
    assert local_obs["model_mapping"] == ["a1", "a2"]


@pytest.mark.asyncio
async def test_dual_backend_config_provider_equivalence(dual_aws_env, monkeypatch):
    """
    The local and AWS ConfigProvider resolve environment-sourced values, defaults
    and required-key errors equivalently (the AWS provider adds an SSM tier on
    top, which is empty here so both fall through to the env > default tail).

    Requirements: 5.5, 10.5
    """
    # A unique key unlikely to exist in the ambient env / .env file.
    key = "DUAL_EQUIV_CONFIG_KEY"
    monkeypatch.setenv(key, "shared-value")

    local_bundle = create_backend("local")

    with mock_aws():
        aws_bundle = create_backend("aws")

        # Present in env (no SSM override): both return the env value.
        assert local_bundle.config_provider.get(key) == "shared-value"
        assert aws_bundle.config_provider.get(key) == "shared-value"
        assert local_bundle.config_provider.get_required(key) == "shared-value"
        assert aws_bundle.config_provider.get_required(key) == "shared-value"

        # Absent key: both return the supplied default.
        assert local_bundle.config_provider.get("MISSING_DUAL_KEY", "d") == "d"
        assert aws_bundle.config_provider.get("MISSING_DUAL_KEY", "d") == "d"

        # Required + absent: both raise MissingConfigError carrying the key name.
        with pytest.raises(MissingConfigError) as local_err:
            local_bundle.config_provider.get_required("MISSING_DUAL_KEY")
        with pytest.raises(MissingConfigError) as aws_err:
            aws_bundle.config_provider.get_required("MISSING_DUAL_KEY")
        assert local_err.value.key == aws_err.value.key == "MISSING_DUAL_KEY"


@pytest.mark.asyncio
async def test_dual_backend_secret_provider_equivalence(dual_aws_env):
    """
    The local and AWS SecretProvider behave equivalently for the write-back
    round-trip, JSON parsing, redaction and the missing-secret error.

    Requirements: 5.5, 10.1
    """
    secret_name = "account/acct-x/token"
    secret_value = json.dumps({"access_token": "tok-abcdef", "refresh_token": "ref-123456"})

    async def _run_secret_ops(secret_provider) -> dict:
        obs: dict = {}
        # Write-back then read-back round-trip (refreshed token persistence).
        await secret_provider.put_secret(secret_name, secret_value)
        obs["round_trip"] = await secret_provider.get_secret(secret_name)
        obs["json_round_trip"] = await secret_provider.get_json_secret(secret_name)
        # Redaction masks the registered secret value and leaves other text.
        line = f"token={secret_value} status=ok"
        redacted = secret_provider.redact(line)
        obs["secret_absent_after_redact"] = secret_value not in redacted
        obs["non_secret_preserved"] = "status=ok" in redacted
        # Missing secret raises KeyError on both backends.
        try:
            await secret_provider.get_secret("account/none/token")
            obs["missing_raises"] = False
        except KeyError:
            obs["missing_raises"] = True
        return obs

    local_bundle = create_backend("local")
    local_obs = await _run_secret_ops(local_bundle.secret_provider)

    with mock_aws():
        aws_bundle = create_backend("aws")
        aws_obs = await _run_secret_ops(aws_bundle.secret_provider)

    assert local_obs == aws_obs, (
        f"secret backend divergence:\n local={local_obs}\n aws  ={aws_obs}"
    )
    assert local_obs["round_trip"] == secret_value
    assert local_obs["json_round_trip"]["access_token"] == "tok-abcdef"
    assert local_obs["secret_absent_after_redact"] is True
    assert local_obs["non_secret_preserved"] is True
    assert local_obs["missing_raises"] is True
