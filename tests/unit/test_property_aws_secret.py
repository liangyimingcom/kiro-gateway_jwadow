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
Property-based test for
:class:`kiro.backends.aws.secret_provider.SecretsManagerProvider`.

Covers design.md "正确性属性" 属性 5 (刷新令牌持久化往返 / refreshed-token
persistence round-trip) against a moto-mocked AWS Secrets Manager service
(``from moto import mock_aws``). Secrets Manager is the *external* dependency
being mocked; the provider's ``put_secret`` write-back and ``get_*`` read path
are the real implementation.

- Task 6.3 -> Property 5 - Validates Requirements 6.7.

Each property runs >= 100 random iterations (Hypothesis ``max_examples=120``).
Every example uses a *fresh* moto Secrets Manager backend and runs the whole
round-trip inside a fresh, empty working directory so that "no local credential
file is written" can be asserted directly.
"""

import asyncio
import json
import os
import shutil
import string
import tempfile
from dataclasses import asdict

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from kiro.backends.aws.secret_provider import SecretsManagerProvider  # noqa: E402
from kiro.backends.interfaces import TokenBundle  # noqa: E402

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

# moto needs *some* AWS credentials/region in the environment; these are dummy
# values consumed only by the in-memory mock (never used against real AWS).
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

REGION = "us-east-1"
STACK = "kiro"

# Account ids restricted to characters valid in a Secrets Manager secret name
# (the provider builds the id "<stack>/account/<id>/token").
_ACCOUNT_ID = st.text(
    alphabet=string.ascii_letters + string.digits + "-_",
    min_size=1,
    max_size=32,
)

_OPT_TEXT = st.one_of(st.none(), st.text(max_size=128))

# A refreshed token bundle: optional string fields plus a finite expires_at
# (NaN / infinity excluded so JSON round-trips exactly).
_TOKEN_BUNDLES = st.builds(
    TokenBundle,
    access_token=_OPT_TEXT,
    refresh_token=_OPT_TEXT,
    expires_at=st.one_of(
        st.none(),
        st.floats(
            min_value=0,
            max_value=4102444800,  # ~year 2100, epoch seconds
            allow_nan=False,
            allow_infinity=False,
        ),
    ),
    profile_arn=_OPT_TEXT,
    region=_OPT_TEXT,
)


# Feature: aws-cloud-native-gateway, Property 5: 对任意账号与刷新后 token 包，经 put_secret 写入后再读回应得到等价 token 包，且不写入任何本地凭证文件
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(account_id=_ACCOUNT_ID, bundle=_TOKEN_BUNDLES)
def test_property_5_refresh_token_persistence_round_trip(account_id, bundle):
    """
    Property 5 (刷新令牌持久化往返 / refreshed-token persistence round-trip): for
    any account and any refreshed token bundle, writing it via ``put_secret`` and
    reading it back yields an equivalent token bundle, and no local credential
    file is written.

    The read cache is disabled (``cache_ttl=0``) so the round-trip is verified
    against the value actually persisted in the (mocked) Secret_Store rather than
    an in-process write-through cache hit.

    "No local credential file is written" is verified by running the whole
    round-trip inside a fresh, empty working directory and asserting nothing was
    created there - the AWS provider must persist only to the Secret_Store
    (Requirements 6.7), never back to a local credential file.

    **Validates: Requirements 6.7**
    """
    secret_name = f"account/{account_id}/token"
    serialized = json.dumps(asdict(bundle))

    original_cwd = os.getcwd()
    isolated_dir = tempfile.mkdtemp(prefix="no_local_cred_")
    os.chdir(isolated_dir)
    try:
        with mock_aws():
            provider = SecretsManagerProvider(stack=STACK, region=REGION, cache_ttl=0)
            # Write the refreshed token back to the Secret_Store ...
            asyncio.run(provider.put_secret(secret_name, serialized))
            # ... then read it back from the persisted Secret_Store value.
            read_back = asyncio.run(provider.get_json_secret(secret_name))

        restored = TokenBundle(**read_back)
        assert restored == bundle, f"round-trip mismatch: {restored!r} != {bundle!r}"

        # No local credential file was written anywhere in the isolated cwd.
        leftover = os.listdir(isolated_dir)
        assert leftover == [], f"unexpected local file(s) written: {leftover}"
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(isolated_dir, ignore_errors=True)
