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
Unit tests for :class:`kiro.backends.aws.debug_sink.S3DebugLogSink`.

These tests exercise the sink's own logic (date-partitioned key construction,
JSON serialisation, request-id sanitisation and non-fatal error handling)
against a lightweight in-memory stand-in for the boto3 S3 client. The fake is a
test double for the *external* AWS service; the gateway logic under test is real.

No ``boto3``/AWS credentials are required: the sink imports ``boto3`` lazily and
the fake client is injected via ``client=...``.
"""

import json
from datetime import datetime, timezone

import pytest

from kiro.backends.aws.debug_sink import S3DebugLogSink
from kiro.backends.interfaces import DebugLogSink


class FakeS3Client:
    """In-memory stand-in for the boto3 S3 client."""

    def __init__(self, fail_with: Exception = None):
        self.objects = {}
        self.put_calls = 0
        self._fail_with = fail_with

    def put_object(self, *, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.put_calls += 1
        if self._fail_with is not None:
            raise self._fail_with
        self.objects[(Bucket, Key)] = {"Body": Body, "ContentType": ContentType}
        return {"ETag": '"fake-etag"'}


def _fixed_clock():
    return datetime(2025, 3, 7, 12, 30, tzinfo=timezone.utc)


# ==================================================================================================
# Protocol conformance
# ==================================================================================================


def test_satisfies_debug_log_sink_protocol():
    sink = S3DebugLogSink("bucket", client=FakeS3Client())
    assert isinstance(sink, DebugLogSink)


# ==================================================================================================
# Key construction
# ==================================================================================================


def test_build_key_is_date_partitioned():
    sink = S3DebugLogSink("bucket", client=FakeS3Client(), now_provider=_fixed_clock)
    assert sink.build_key("req-123") == "debug/2025/03/07/req-123.json"


def test_build_key_honours_custom_prefix():
    sink = S3DebugLogSink(
        "bucket", prefix="archive/dbg", client=FakeS3Client(), now_provider=_fixed_clock
    )
    assert sink.build_key("req-1") == "archive/dbg/2025/03/07/req-1.json"


def test_build_key_sanitizes_request_id():
    sink = S3DebugLogSink("bucket", client=FakeS3Client(), now_provider=_fixed_clock)
    # Path separators must not create unintended S3 prefixes / traversal.
    assert sink.build_key("a/b\\c") == "debug/2025/03/07/a_b_c.json"


def test_empty_bucket_is_rejected():
    with pytest.raises(ValueError):
        S3DebugLogSink("")


# ==================================================================================================
# write()
# ==================================================================================================


@pytest.mark.asyncio
async def test_write_archives_payload_to_expected_key():
    client = FakeS3Client()
    sink = S3DebugLogSink("debug-bucket", client=client, now_provider=_fixed_clock)
    payload = {"request_id": "req-9", "model": "kiro", "messages": [1, 2, 3]}

    await sink.write("req-9", payload)

    key = "debug/2025/03/07/req-9.json"
    assert client.put_calls == 1
    stored = client.objects[("debug-bucket", key)]
    assert stored["ContentType"] == "application/json"
    # Body round-trips to the original payload.
    assert json.loads(stored["Body"].decode("utf-8")) == payload


@pytest.mark.asyncio
async def test_write_preserves_non_ascii_content():
    client = FakeS3Client()
    sink = S3DebugLogSink("b", client=client, now_provider=_fixed_clock)

    await sink.write("req", {"note": "测试-débug"})

    stored = next(iter(client.objects.values()))
    assert json.loads(stored["Body"].decode("utf-8")) == {"note": "测试-débug"}


@pytest.mark.asyncio
async def test_write_errors_are_non_fatal():
    # A failing S3 client must never propagate into the request path.
    client = FakeS3Client(fail_with=RuntimeError("S3 unavailable"))
    sink = S3DebugLogSink("b", client=client, now_provider=_fixed_clock)

    # Should not raise.
    await sink.write("req-err", {"x": 1})
    assert client.put_calls == 1


@pytest.mark.asyncio
async def test_write_does_not_require_boto3_when_client_injected():
    # With an injected client the lazy boto3 import path is never taken.
    client = FakeS3Client()
    sink = S3DebugLogSink("b", client=client, now_provider=_fixed_clock)
    await sink.write("r", {"k": "v"})
    assert client.put_calls == 1
