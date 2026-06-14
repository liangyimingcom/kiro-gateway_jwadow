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
Unit tests for the AWS configuration providers (SsmConfigProvider /
S3ConfigProvider), exercised against moto-mocked SSM and S3 services.

These are example-based tests covering the configuration precedence
(SSM > env > default), required-key validation, and S3 credentials-skeleton
loading. The full property-based tests for precedence (Property 1) and missing
keys (Property 2) are separate, optional tasks.
"""

import json

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from kiro.backends.aws.config_provider import S3ConfigProvider, SsmConfigProvider  # noqa: E402
from kiro.backends.interfaces import MissingConfigError  # noqa: E402

REGION = "us-east-1"
STACK = "teststack"


@pytest.fixture
def ssm_client():
    with mock_aws():
        client = boto3.client("ssm", region_name=REGION)
        yield client


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        yield client


def _put_param(client, name, value):
    client.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


# ==================================================================================================
# SsmConfigProvider
# ==================================================================================================


def test_ssm_value_takes_precedence_over_env(ssm_client, monkeypatch):
    _put_param(ssm_client, f"/{STACK}/config/STREAMING_READ_TIMEOUT", "300")
    monkeypatch.setenv("STREAMING_READ_TIMEOUT", "999")

    provider = SsmConfigProvider(STACK, region_name=REGION, client=ssm_client)

    # SSM explicit value wins over the environment variable.
    assert provider.get("STREAMING_READ_TIMEOUT") == "300"


def test_env_used_when_absent_in_ssm(ssm_client, monkeypatch):
    monkeypatch.setenv("FIRST_TOKEN_TIMEOUT", "15")
    provider = SsmConfigProvider(STACK, region_name=REGION, client=ssm_client)

    # Falls through SSM (empty) to the environment variable.
    assert provider.get("FIRST_TOKEN_TIMEOUT") == "15"


def test_default_used_when_absent_everywhere(ssm_client, monkeypatch):
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    provider = SsmConfigProvider(STACK, region_name=REGION, client=ssm_client)

    assert provider.get("DOES_NOT_EXIST", "fallback") == "fallback"
    assert provider.get("DOES_NOT_EXIST") is None


def test_get_required_raises_missing_config_error(ssm_client, monkeypatch):
    monkeypatch.delenv("REQUIRED_MISSING", raising=False)
    provider = SsmConfigProvider(STACK, region_name=REGION, client=ssm_client)

    with pytest.raises(MissingConfigError) as exc_info:
        provider.get_required("REQUIRED_MISSING")
    # The error names the missing key for diagnosability (Requirements 5.4).
    assert exc_info.value.key == "REQUIRED_MISSING"
    assert "REQUIRED_MISSING" in str(exc_info.value)


def test_get_required_returns_present_value(ssm_client):
    _put_param(ssm_client, f"/{STACK}/config/PRESENT_KEY", "value-1")
    provider = SsmConfigProvider(STACK, region_name=REGION, client=ssm_client)

    assert provider.get_required("PRESENT_KEY") == "value-1"


def test_get_namespace_returns_nested_keys(ssm_client):
    _put_param(ssm_client, f"/{STACK}/config/model-aliases/fast", "kiro-fast")
    _put_param(ssm_client, f"/{STACK}/config/model-aliases/smart", "kiro-smart")
    _put_param(ssm_client, f"/{STACK}/config/OTHER", "x")
    provider = SsmConfigProvider(STACK, region_name=REGION, client=ssm_client)

    namespace = provider.get_namespace("model-aliases/")
    assert namespace == {
        "model-aliases/fast": "kiro-fast",
        "model-aliases/smart": "kiro-smart",
    }


# ==================================================================================================
# S3ConfigProvider
# ==================================================================================================


def _make_bucket(client, bucket):
    client.create_bucket(Bucket=bucket)


def test_s3_loads_credentials_skeleton(s3_client):
    bucket = "config-bucket"
    _make_bucket(s3_client, bucket)
    skeleton = [
        {"type": "refresh_token", "refresh_token": "tok", "region": "us-east-1"},
        {"type": "json", "path": "~/.aws/sso/cache/a.json"},
    ]
    s3_client.put_object(
        Bucket=bucket,
        Key="config/credentials.json",
        Body=json.dumps(skeleton).encode("utf-8"),
    )

    provider = S3ConfigProvider(bucket, region_name=REGION, client=s3_client)
    loaded = provider.get_credentials()

    assert loaded == skeleton
    # Cached on the second call (no second S3 fetch needed).
    assert provider.get_credentials() is loaded


def test_s3_missing_object_raises_missing_config_error(s3_client):
    bucket = "config-bucket"
    _make_bucket(s3_client, bucket)
    provider = S3ConfigProvider(bucket, region_name=REGION, client=s3_client)

    with pytest.raises(MissingConfigError) as exc_info:
        provider.get_credentials()
    assert "credentials.json" in str(exc_info.value)


def test_s3_empty_skeleton_is_rejected(s3_client):
    bucket = "config-bucket"
    _make_bucket(s3_client, bucket)
    s3_client.put_object(
        Bucket=bucket, Key="config/credentials.json", Body=b"[]"
    )
    provider = S3ConfigProvider(bucket, region_name=REGION, client=s3_client)

    with pytest.raises(MissingConfigError):
        provider.get_required_credentials()


def test_s3_non_list_json_is_rejected(s3_client):
    bucket = "config-bucket"
    _make_bucket(s3_client, bucket)
    s3_client.put_object(
        Bucket=bucket, Key="config/credentials.json", Body=b'{"not": "a list"}'
    )
    provider = S3ConfigProvider(bucket, region_name=REGION, client=s3_client)

    with pytest.raises(ValueError):
        provider.get_credentials()


def test_import_does_not_require_aws_credentials():
    # Constructing providers must not create clients or touch AWS.
    ssm = SsmConfigProvider(STACK)
    s3 = S3ConfigProvider("some-bucket")
    assert ssm._client is None
    assert s3._client is None
    assert s3.uri == "s3://some-bucket/config/credentials.json"
