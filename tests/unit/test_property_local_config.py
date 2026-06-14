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
Property-based tests for :class:`kiro.backends.local.config_provider.LocalConfigProvider`.

Covers two design correctness properties (see design.md "正确性属性"):

- Property 1 (配置来源优先级 / configuration source precedence) - Requirements 5.3
- Property 2 (必需配置缺失触发明确错误 / missing required key raises a clear
  error) - Requirements 5.4

For the *local* backend the configuration layers reduce to "environment
variable > code default" (there is no SSM layer in the local provider; the full
SSM > env > default precedence is realised by the AWS provider). These tests
therefore exercise the env > default tail of the precedence chain.

Environment isolation: every generated key lives under a dedicated, collision-
proof prefix so the tests can never read or clobber a real gateway
configuration key, and ``monkeypatch`` is used for all env mutations so the
process environment is fully restored afterwards. The provider is constructed
with a guaranteed-missing ``.env`` path so ``reload()`` is a no-op and never
pollutes ``os.environ``.
"""

import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro.backends.interfaces import MissingConfigError
from kiro.backends.local.config_provider import LocalConfigProvider

# A ``.env`` path that does not exist: load_dotenv() treats a missing file as a
# no-op, so constructing the provider never touches the process environment.
_NONEXISTENT_ENV = "__kiro_pbt_nonexistent__.env"

# Dedicated namespaces that cannot collide with real gateway configuration keys
# (e.g. PROXY_API_KEY, REFRESH_TOKEN, STREAMING_READ_TIMEOUT, ...).
_PRIORITY_PREFIX = "KIRO_PBT_CFG_"
_REQUIRED_PREFIX = "KIRO_PBT_REQ_"

# Valid environment-variable name suffix: upper-case letters, digits, underscore.
_key_suffix = st.text(
    alphabet=string.ascii_uppercase + string.digits + "_",
    min_size=1,
    max_size=20,
)

# Environment-variable values: printable, single-line text (no NUL / newline /
# control chars, which some platforms reject in os.environ values).
_printable = st.characters(min_codepoint=32, max_codepoint=0x10FFFF, blacklist_categories=("Cs",))
_value = st.text(alphabet=_printable, min_size=0, max_size=50)


def _make_provider() -> LocalConfigProvider:
    """Build a provider whose reload() cannot pollute the environment."""
    return LocalConfigProvider(env_file=_NONEXISTENT_ENV)


# ==================================================================================================
# Property 1: configuration source precedence (Requirements 5.3)
# ==================================================================================================


# Feature: aws-cloud-native-gateway, Property 1: 对任意配置键及其在各层存在性与取值的任意组合，ConfigProvider.get 返回最高优先级层的取值（SSM > 环境变量 > 默认值）
# **Validates: Requirements 5.3**
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    suffix=_key_suffix,
    env_present=st.booleans(),
    env_value=_value,
    default=st.one_of(st.none(), _value),
)
def test_get_returns_highest_priority_layer(monkeypatch, suffix, env_present, env_value, default):
    """For any key and any combination of layer presence/values, ``get`` returns
    the value of the highest-priority *present* layer (env var > default)."""
    key = _PRIORITY_PREFIX + suffix

    if env_present:
        # Highest-priority layer is present -> it must win over the default.
        monkeypatch.setenv(key, env_value)
    else:
        # Environment layer absent -> the default must surface (possibly None).
        monkeypatch.delenv(key, raising=False)

    provider = _make_provider()

    if env_present:
        assert provider.get(key, default) == env_value
        # The default is irrelevant when the higher-priority layer is present.
        assert provider.get(key) == env_value
    else:
        assert provider.get(key, default) == default
        assert provider.get(key) is None


# ==================================================================================================
# Property 2: missing required key raises a clear error (Requirements 5.4)
# ==================================================================================================


# Feature: aws-cloud-native-gateway, Property 2: 对任意必需配置键集合与其任意缺失子集，对缺失键调用 get_required 抛出含键名的 MissingConfigError，对存在键正常返回
# **Validates: Requirements 5.4**
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    # Map each key suffix to either None (missing) or a concrete value (present).
    spec=st.dictionaries(
        keys=_key_suffix,
        values=st.one_of(st.none(), _value),
        min_size=0,
        max_size=8,
    ),
)
def test_get_required_missing_raises_present_returns(monkeypatch, spec):
    """For any set of required keys and any missing subset, ``get_required``
    raises ``MissingConfigError`` (naming the key) for missing keys and returns
    the value for present keys."""
    # Establish the desired environment state for every key in this example.
    for suffix, maybe_value in spec.items():
        key = _REQUIRED_PREFIX + suffix
        if maybe_value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, maybe_value)

    provider = _make_provider()

    for suffix, maybe_value in spec.items():
        key = _REQUIRED_PREFIX + suffix
        if maybe_value is None:
            with pytest.raises(MissingConfigError) as exc_info:
                provider.get_required(key)
            # The error must name the missing key for operator diagnosability.
            assert exc_info.value.key == key
            assert key in str(exc_info.value)
        else:
            # Present keys (including empty-string values) return their value.
            assert provider.get_required(key) == maybe_value


# ==================================================================================================
# Complementary example-based unit tests
# ==================================================================================================


def test_env_var_wins_over_default(monkeypatch):
    monkeypatch.setenv("KIRO_PBT_CFG_EXAMPLE", "from-env")
    provider = _make_provider()
    assert provider.get("KIRO_PBT_CFG_EXAMPLE", "from-default") == "from-env"


def test_default_used_when_env_absent(monkeypatch):
    monkeypatch.delenv("KIRO_PBT_CFG_ABSENT", raising=False)
    provider = _make_provider()
    assert provider.get("KIRO_PBT_CFG_ABSENT", "fallback") == "fallback"
    assert provider.get("KIRO_PBT_CFG_ABSENT") is None


def test_empty_string_env_is_present_not_missing(monkeypatch):
    # An empty value is still "present": get_required must not raise.
    monkeypatch.setenv("KIRO_PBT_REQ_EMPTY", "")
    provider = _make_provider()
    assert provider.get_required("KIRO_PBT_REQ_EMPTY") == ""


def test_get_required_missing_names_key(monkeypatch):
    monkeypatch.delenv("KIRO_PBT_REQ_MISSING", raising=False)
    provider = _make_provider()
    with pytest.raises(MissingConfigError) as exc_info:
        provider.get_required("KIRO_PBT_REQ_MISSING")
    assert exc_info.value.key == "KIRO_PBT_REQ_MISSING"
    assert "KIRO_PBT_REQ_MISSING" in str(exc_info.value)
