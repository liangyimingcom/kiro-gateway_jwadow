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
Integration test (Task 11.1): moto end-to-end against the *assembled* AWS
backend.

This exercises the cloud-native storage backend as a whole - the bundle returned
by :func:`kiro.backends.factory.create_backend` with ``STORAGE_BACKEND=aws`` -
against moto-mocked AWS services (DynamoDB / Secrets Manager / SSM Parameter
Store / S3, all covered by the unified ``from moto import mock_aws``).

Rather than spinning up the full uvicorn HTTP server (which would require live
upstream sockets and is inherently flaky), it drives the same I/O-boundary chain
the request path relies on, end to end (design.md "测试策略 → 集成测试"):

    配置加载 (SsmConfigProvider)            -> config load
      -> 账号选择 (DynamoStateStore)        -> account selection from shared state
      -> (模拟) 上游                         -> mocked upstream call
      -> 状态写回 (DynamoDB + Secrets)       -> failure reset / stats / model map / token write-back
      -> 调试归档 (S3DebugLogSink)           -> per-request debug archival

Cross-instance visibility (Requirement 7.3) is verified by reading the written
state back through a *separate*, cache-less ``DynamoStateStore`` pointed at the
same table - modelling a second Gateway instance in the Fargate pool.

Requirements: 1.4 (stateless gateway, state externalised), 5.5 (config source
swapped without changing API behaviour), 7.3 (state visible across instances).
"""

import asyncio
import json
import os

import pytest

# AWS backend integration deps. Skip cleanly if unavailable.
boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from kiro.backends.aws.state_store import (  # noqa: E402
    PK_ATTR,
    SK_ATTR,
    DynamoStateStore,
)
from kiro.backends.factory import create_backend  # noqa: E402
from kiro.backends.interfaces import MissingConfigError, TokenBundle  # noqa: E402

# moto needs *some* AWS credentials/region present; these dummies are consumed
# only by the in-memory mock and never reach real AWS.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

# Deployment identifiers used to address the (mocked) AWS resources. Kept local
# to this module; the fixture wires them into the environment and restores them.
REGION = "us-east-1"
STACK = "kiro-itest"
STATE_TABLE = f"{STACK}-gateway-state"
DEBUG_BUCKET = f"{STACK}-gateway-debug"
CONFIG_BUCKET = f"{STACK}-gateway-config"

# A non-sensitive config key seeded into SSM and a per-account token / secrets.
SSM_KEY = "STREAMING_READ_TIMEOUT"
SSM_VALUE = "600"
HEALTHY_ACCOUNT = "acct-healthy"
FAILED_ACCOUNT = "acct-failed"
MODEL_NAME = "claude-sonnet-4-5"
PROXY_API_KEY_VALUE = "itest-proxy-key-abcdef0123456789"


@pytest.fixture
def aws_backend_env(monkeypatch):
    """
    Set the deployment environment variables ``create_backend('aws')`` reads, and
    restore them afterwards (``monkeypatch`` auto-reverts) so ``STORAGE_BACKEND``
    and friends never leak into other tests.
    """
    monkeypatch.setenv("STORAGE_BACKEND", "aws")
    monkeypatch.setenv("STACK_NAME", STACK)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("GATEWAY_STATE_TABLE", STATE_TABLE)
    monkeypatch.setenv("DEBUG_S3_BUCKET", DEBUG_BUCKET)
    # Keep the state read-cache enabled (its default) - this is the cloud form.
    monkeypatch.delenv("STATE_CACHE_TTL_SECONDS", raising=False)
    yield


def _provision_aws_resources():
    """
    Create the moto-mocked AWS resources the assembled AWS backend binds to.

    Must be called inside an active ``mock_aws()`` context. Creates:

    - DynamoDB single table (PK/SK schema matching ``DynamoStateStore``),
    - SSM parameter ``/<stack>/config/<SSM_KEY>``,
    - Secrets Manager secrets for ``PROXY_API_KEY`` and one account token,
    - S3 buckets for debug archival and the credentials skeleton.
    """
    # --- State_Store: DynamoDB single table ---
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

    # --- Config_Store: SSM Parameter Store under /<stack>/config ---
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(
        Name=f"/{STACK}/config/{SSM_KEY}",
        Value=SSM_VALUE,
        Type="String",
    )

    # --- Secret_Store: Secrets Manager (PROXY_API_KEY + per-account token) ---
    secrets = boto3.client("secretsmanager", region_name=REGION)
    secrets.create_secret(Name=f"{STACK}/proxy-api-key", SecretString=PROXY_API_KEY_VALUE)
    secrets.create_secret(
        Name=f"{STACK}/account/{HEALTHY_ACCOUNT}/token",
        SecretString=json.dumps(
            {
                "access_token": "initial-access-token",
                "refresh_token": "initial-refresh-token",
                "expires_at": 1_700_000_000.0,
            }
        ),
    )

    # --- Object_Store: S3 buckets (debug archival + credentials skeleton) ---
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=DEBUG_BUCKET)
    s3.create_bucket(Bucket=CONFIG_BUCKET)


async def _select_healthy_account(state_store, candidates):
    """
    Minimal account selection over shared state: read each candidate's state from
    the State_Store and return the first with no recorded failures.

    This is the integration point that matters here - account selection is driven
    by state read back from DynamoDB - not the full circuit-breaker formula
    (which has its own dedicated property tests).
    """
    for account_id in candidates:
        state = await state_store.get_account_state(account_id)
        if state.failures == 0:
            return account_id
    return None


@pytest.mark.asyncio
async def test_e2e_aws_backend_full_chain(aws_backend_env):
    """
    Drive config load -> account selection -> mocked upstream -> state write-back
    -> debug archival end-to-end against the assembled AWS backend, then verify
    the write-back is visible to a second (cache-less) store instance.

    Requirements: 1.4, 5.5, 7.3
    """
    with mock_aws():
        _provision_aws_resources()

        # Assemble the real AWS bundle (SSM/Secrets/DynamoDB+cache/S3). Built
        # inside the running loop so the stores' asyncio.Lock binds correctly.
        bundle = create_backend("aws")
        assert bundle.name == "aws"

        # --- 1) Config load (SsmConfigProvider) -------------------------------
        # Value present in SSM is returned; precedence SSM > env > default holds.
        assert bundle.config_provider.get_required(SSM_KEY) == SSM_VALUE
        # A key absent everywhere surfaces a clear MissingConfigError (Req 5.4).
        with pytest.raises(MissingConfigError):
            bundle.config_provider.get_required("DEFINITELY_MISSING_KEY")

        # --- Secrets: client auth key reachable from Secret_Store -------------
        proxy_key = await bundle.secret_provider.get_secret("PROXY_API_KEY")
        assert proxy_key == PROXY_API_KEY_VALUE

        # --- 2) Account selection (DynamoStateStore via cache) ----------------
        # Seed one account as failed/in-cooldown; selection must skip it.
        await bundle.state_store.increment_failure(FAILED_ACCOUNT, failure_time=123.0)
        selected = await _select_healthy_account(
            bundle.state_store, [FAILED_ACCOUNT, HEALTHY_ACCOUNT]
        )
        assert selected == HEALTHY_ACCOUNT

        # --- 3) (mocked) upstream call ----------------------------------------
        # Stand in for the Kiro upstream; the response is irrelevant to the
        # storage chain, only that it "succeeded" so we drive the success path.
        async def _mock_upstream(account_id: str) -> dict:
            return {"account": account_id, "status": "ok", "content": "hello"}

        upstream = await _mock_upstream(selected)
        assert upstream["status"] == "ok"

        # --- 4) State write-back (DynamoDB + Secrets) -------------------------
        # Success path: record stats, clear failures, register model->account,
        # and write the refreshed token back to the Secret_Store (not a file).
        await bundle.state_store.incr_stats(selected, total=1, ok=1, failed=0)
        await bundle.state_store.reset_failure(selected)
        await bundle.state_store.add_model_account(MODEL_NAME, selected)

        refreshed = TokenBundle(
            access_token="refreshed-access-token",
            refresh_token="refreshed-refresh-token",
            expires_at=1_800_000_000.0,
        )
        await bundle.secret_provider.put_secret(
            f"account/{selected}/token", json.dumps(refreshed.__dict__)
        )

        # --- 5) Debug archival to S3 (S3DebugLogSink) -------------------------
        request_id = "itest-req-0001"
        await bundle.debug_sink.write(
            request_id, {"request_id": request_id, "account": selected, "model": MODEL_NAME}
        )

        # ===== Verify write-back is visible to a SEPARATE store instance ======
        # A fresh DynamoStateStore (no cache) models a second Gateway instance
        # reading the authoritative shared state (Requirement 7.3).
        other_instance = DynamoStateStore(table_name=STATE_TABLE, region_name=REGION)
        persisted = await other_instance.get_account_state(selected)
        assert persisted.failures == 0
        assert persisted.stats.total_requests == 1
        assert persisted.stats.successful_requests == 1
        assert persisted.stats.failed_requests == 0

        model_accounts = await other_instance.get_model_mapping(MODEL_NAME)
        assert selected in model_accounts

        # The failed account remains in cooldown for the other instance too.
        failed_seen = await other_instance.get_account_state(FAILED_ACCOUNT)
        assert failed_seen.failures == 1

        # Refreshed token round-trips through the Secret_Store (no local file).
        token_back = await bundle.secret_provider.get_json_secret(
            f"account/{selected}/token"
        )
        assert token_back["access_token"] == "refreshed-access-token"

        # Debug payload landed in S3 under the date-partitioned key layout.
        s3 = boto3.client("s3", region_name=REGION)
        listing = s3.list_objects_v2(Bucket=DEBUG_BUCKET)
        keys = [obj["Key"] for obj in listing.get("Contents", [])]
        assert any(request_id in key for key in keys), keys


@pytest.mark.asyncio
async def test_e2e_aws_backend_config_precedence_ssm_over_env(aws_backend_env, monkeypatch):
    """
    Config precedence: an SSM value wins over an equally-named environment
    variable, while a key present only in the environment is still resolved.

    Requirements: 5.5 (config externalised without behaviour change)
    """
    with mock_aws():
        _provision_aws_resources()
        # Same key also set in the environment with a different value: SSM wins.
        monkeypatch.setenv(SSM_KEY, "env-loses")
        # A key that exists *only* in the environment must still resolve.
        monkeypatch.setenv("ENV_ONLY_KEY", "env-wins-here")

        bundle = create_backend("aws")
        assert bundle.config_provider.get(SSM_KEY) == SSM_VALUE       # SSM precedence
        assert bundle.config_provider.get("ENV_ONLY_KEY") == "env-wins-here"
        assert bundle.config_provider.get("ABSENT", "fallback") == "fallback"
