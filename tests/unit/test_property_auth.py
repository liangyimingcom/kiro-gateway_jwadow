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
Property-based test for the gateway's client authentication predicate.

Covers design.md "正确性属性" 属性 4 (客户端鉴权匹配 / client auth matching).
The *real* auth dependencies used by the routes are exercised (no mocking of the
auth logic):

- :func:`kiro.routes_openai.verify_api_key`            (``Authorization: Bearer``)
- :func:`kiro.routes_anthropic.verify_anthropic_api_key`(``x-api-key`` / ``Authorization: Bearer``)

Both accept a submitted key IF AND ONLY IF it equals the configured Secret_Store
``PROXY_API_KEY``; otherwise they reject the request with an auth-failure status
code (HTTP 401).

- Task 6.4 -> Property 4 - Validates Requirements 6.6, 10.4.

Each property runs >= 100 random iterations (Hypothesis ``max_examples=200``).
"""

import asyncio

import pytest
from fastapi import HTTPException

import kiro.routes_anthropic as routes_anthropic
import kiro.routes_openai as routes_openai

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# A fixed, definitely-non-empty configured PROXY_API_KEY so the predicate has a
# deterministic ground-truth value to compare submitted keys against.
CONFIGURED_KEY = "configured-proxy-secret-key-9f3a7c"

# The auth-failure status code returned by both dependencies on rejection.
AUTH_FAILURE_STATUS = 401


@pytest.fixture(scope="module", autouse=True)
def _configure_proxy_api_key():
    """
    Pin the module-global ``PROXY_API_KEY`` that both auth dependencies compare
    against to a known value for the duration of this module, restoring the
    originals afterwards.

    Module scope is intentional: function-scoped fixtures combined with
    Hypothesis' ``@given`` only run once and would trip a health check.
    """
    old_openai = routes_openai.PROXY_API_KEY
    old_anthropic = routes_anthropic.PROXY_API_KEY
    routes_openai.PROXY_API_KEY = CONFIGURED_KEY
    routes_anthropic.PROXY_API_KEY = CONFIGURED_KEY
    try:
        yield
    finally:
        routes_openai.PROXY_API_KEY = old_openai
        routes_anthropic.PROXY_API_KEY = old_anthropic


def _auth_outcome(coro_factory):
    """
    Run an auth dependency coroutine and report the outcome.

    Returns ``(passed, status_code)``: ``passed`` is True when the dependency
    returns truthy without raising; otherwise ``passed`` is False and
    ``status_code`` carries the HTTP status from the raised ``HTTPException``.
    """
    try:
        result = asyncio.run(coro_factory())
    except HTTPException as exc:
        return False, exc.status_code
    return bool(result), None


# Submitted keys: bias towards the exact configured key (to exercise the accept
# branch) plus arbitrary printable text (to exercise the reject branch). Limited
# to printable, non-space header-safe characters so the auth predicate - not
# header value construction - is what is under test.
_SUBMITTED_KEYS = st.one_of(
    st.just(CONFIGURED_KEY),
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        max_size=64,
    ),
)


# Feature: aws-cloud-native-gateway, Property 4: 对任意 Authorization/x-api-key 提交的密钥，当且仅当其等于 Secret_Store 中 PROXY_API_KEY 时鉴权通过，否则返回鉴权失败状态码
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(submitted=_SUBMITTED_KEYS)
def test_property_4_client_auth_matches_proxy_api_key(submitted):
    """
    Property 4 (客户端鉴权匹配 / client auth matching): for any submitted key,
    each real route auth dependency accepts the request IF AND ONLY IF the
    submitted key equals the configured ``PROXY_API_KEY``; otherwise it rejects
    with the auth-failure status code (HTTP 401).

    Drives the genuine predicates for every supported header form:
      1. OpenAI:    ``Authorization: Bearer <submitted>``
      2. Anthropic: ``x-api-key: <submitted>``
      3. Anthropic: ``Authorization: Bearer <submitted>``

    **Validates: Requirements 6.6, 10.4**
    """
    expected_pass = submitted == CONFIGURED_KEY

    # 1. OpenAI Authorization: Bearer header.
    openai_pass, openai_status = _auth_outcome(
        lambda: routes_openai.verify_api_key(auth_header=f"Bearer {submitted}")
    )
    # 2. Anthropic native x-api-key header.
    anthropic_xkey_pass, anthropic_xkey_status = _auth_outcome(
        lambda: routes_anthropic.verify_anthropic_api_key(
            x_api_key=submitted, authorization=None
        )
    )
    # 3. Anthropic Authorization: Bearer header (compatibility path).
    anthropic_bearer_pass, anthropic_bearer_status = _auth_outcome(
        lambda: routes_anthropic.verify_anthropic_api_key(
            x_api_key=None, authorization=f"Bearer {submitted}"
        )
    )

    if expected_pass:
        # Submitted key equals PROXY_API_KEY -> every header form is accepted.
        assert openai_pass is True
        assert anthropic_xkey_pass is True
        assert anthropic_bearer_pass is True
    else:
        # Any other submitted key -> rejected with the auth-failure status code.
        assert openai_pass is False and openai_status == AUTH_FAILURE_STATUS
        assert (
            anthropic_xkey_pass is False
            and anthropic_xkey_status == AUTH_FAILURE_STATUS
        )
        assert (
            anthropic_bearer_pass is False
            and anthropic_bearer_status == AUTH_FAILURE_STATUS
        )
