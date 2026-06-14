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
Storage backend interfaces and shared data types.

Defines the five core abstractions used to decouple the gateway from any
concrete storage technology (see design.md "组件与接口"):

1. :class:`ConfigProvider`           - non-sensitive configuration
2. :class:`SecretProvider`           - sensitive credentials + log redaction
3. :class:`StateStore`               - shared runtime state (atomic updates)
4. :class:`TokenRefreshCoordinator`  - distributed single-flight token refresh
5. :class:`DebugLogSink`             - debug log archival

The interfaces are declared with :class:`typing.Protocol` so that concrete
implementations only need to be structurally compatible (no inheritance
required). They are marked ``@runtime_checkable`` to permit ``isinstance``
checks in the factory and tests.

Shared data types (:class:`AccountState`, :class:`TokenBundle`) and the
:class:`MissingConfigError` exception are also defined here so that both the
``local`` and ``aws`` implementations depend on a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


# ==================================================================================================
# Exceptions
# ==================================================================================================


class MissingConfigError(KeyError):
    """
    Raised when a required configuration key is absent from all configuration
    sources (Config_Store, environment variables and code defaults).

    Inherits from :class:`KeyError` for backward-compatible exception handling,
    while exposing the missing key name explicitly via :attr:`key`.

    The string representation always contains the key name so that operators can
    diagnose start-up failures from logs (Requirements 5.4).
    """

    def __init__(self, key: str, message: Optional[str] = None) -> None:
        self.key = key
        if message is None:
            message = f"Required configuration key is missing: '{key}'"
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# ==================================================================================================
# Shared data types
# ==================================================================================================


@dataclass
class AccountStatsState:
    """
    Usage statistics for a single account.

    Mirrors the application-level ``AccountStats`` dataclass in
    :mod:`kiro.account_manager`, but lives in the storage layer so that the
    backend implementations do not depend on the application module.
    """

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0


@dataclass
class AccountState:
    """
    Shared runtime state for a single account, as persisted in the State_Store.

    This is the storage-layer projection of the per-account fields that today
    live in ``state.json`` (failure counter, circuit-breaker timing, model cache
    timestamp and usage statistics). See design.md "应用内状态数据类".
    """

    account_id: str
    failures: int = 0
    last_failure_time: float = 0.0
    models_cached_at: float = 0.0
    stats: AccountStatsState = field(default_factory=AccountStatsState)


@dataclass
class TokenBundle:
    """
    A refreshed authentication token together with its metadata.

    Used by :class:`SecretProvider` and :class:`TokenRefreshCoordinator` to move
    refreshed tokens between the gateway and the Secret_Store / State_Store
    without writing back to local credential files (Requirements 6.7).

    Attributes:
        access_token: Current access token (sensitive).
        refresh_token: Refresh token used to obtain new access tokens (sensitive).
        expires_at: Token expiry as epoch seconds (UTC), or ``None`` if unknown.
        profile_arn: AWS CodeWhisperer profile ARN associated with the account.
        region: SSO / API region associated with the credentials.
    """

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None
    profile_arn: Optional[str] = None
    region: Optional[str] = None


# ==================================================================================================
# Interface 1: ConfigProvider
# ==================================================================================================


@runtime_checkable
class ConfigProvider(Protocol):
    """
    Provides non-sensitive configuration (ports, timeouts, model mappings,
    scaling parameters, ...) and enforces the configuration precedence and
    required-key validation rules.

    Precedence (highest first): Config_Store explicit value > environment
    variable > code default (Requirements 5.3).
    """

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Return the value for ``key`` or ``default`` if it is not present."""
        ...

    def get_required(self, key: str) -> str:
        """
        Return the value for ``key``.

        Raises:
            MissingConfigError: If the key is absent from all configuration
                sources (Requirements 5.4).
        """
        ...

    def get_namespace(self, prefix: str) -> dict[str, str]:
        """Return all key/value pairs whose key starts with ``prefix`` (e.g. model mappings)."""
        ...

    def reload(self) -> None:
        """Reload configuration from the underlying source (effective after restart / redeploy)."""
        ...


# ==================================================================================================
# Interface 2: SecretProvider
# ==================================================================================================


@runtime_checkable
class SecretProvider(Protocol):
    """
    Reads sensitive values (``PROXY_API_KEY``, credentials, refresh/access
    tokens), supports writing back refreshed tokens, and provides a redacted
    view of arbitrary text for logging (Requirements 6.x).
    """

    async def get_secret(self, name: str) -> str:
        """Return the secret value for ``name``."""
        ...

    async def get_json_secret(self, name: str) -> dict:
        """Return the secret value for ``name`` parsed as JSON."""
        ...

    async def put_secret(self, name: str, value: str) -> None:
        """Persist ``value`` for ``name`` (e.g. a refreshed token)."""
        ...

    def redact(self, text: str) -> str:
        """Return ``text`` with all known secret values masked (Requirements 6.5)."""
        ...


# ==================================================================================================
# Interface 3: StateStore
# ==================================================================================================


@runtime_checkable
class StateStore(Protocol):
    """
    Abstracts read/write and atomic updates of shared account runtime state
    (Requirements 7.x).
    """

    async def get_account_state(self, account_id: str) -> AccountState:
        """Return the current shared state for ``account_id`` (empty state if unknown)."""
        ...

    async def increment_failure(self, account_id: str, failure_time: float) -> AccountState:
        """Atomically increment the failure counter and set the last failure time."""
        ...

    async def reset_failure(self, account_id: str) -> None:
        """Reset the failure counter for ``account_id`` to zero (success path)."""
        ...

    async def incr_stats(self, account_id: str, *, total: int, ok: int, failed: int) -> None:
        """Atomically increment the usage statistics counters."""
        ...

    async def get_sticky_index(self) -> int:
        """Return the global sticky account index."""
        ...

    async def set_sticky_index(self, index: int) -> None:
        """Set the global sticky account index."""
        ...

    async def get_model_mapping(self, model: str) -> list[str]:
        """Return the list of account IDs known to serve ``model``."""
        ...

    async def add_model_account(self, model: str, account_id: str) -> None:
        """Idempotently add ``account_id`` to the account list for ``model``."""
        ...


# ==================================================================================================
# Interface 4: TokenRefreshCoordinator
# ==================================================================================================


@runtime_checkable
class TokenRefreshCoordinator(Protocol):
    """
    Encapsulates distributed single-flight token refresh so that, within a
    refresh window, only one instance actually refreshes a given account's token
    while the others reuse the result (Requirements 6.7, 7.7).
    """

    async def acquire_lock(self, account_id: str, ttl_seconds: int) -> bool:
        """Attempt to acquire the refresh lease lock for ``account_id``."""
        ...

    async def release_lock(self, account_id: str) -> None:
        """Release the refresh lease lock for ``account_id``."""
        ...

    async def store_refreshed_token(self, account_id: str, token: TokenBundle) -> None:
        """Persist a refreshed token so other instances can reuse it."""
        ...

    async def wait_for_token(self, account_id: str, timeout: float) -> Optional[TokenBundle]:
        """Wait (bounded by ``timeout``) for a refreshed token produced by the lock holder."""
        ...


# ==================================================================================================
# Interface 5: DebugLogSink
# ==================================================================================================


@runtime_checkable
class DebugLogSink(Protocol):
    """
    Sink for per-request debug logs (Requirements 8.2).
    """

    async def write(self, request_id: str, payload: dict) -> None:
        """Persist the debug ``payload`` associated with ``request_id``."""
        ...
