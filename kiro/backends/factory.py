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
Backend factory.

Selects and assembles a complete set of storage backend implementations based on
the ``STORAGE_BACKEND`` environment variable (``local`` | ``aws``) and returns
them bundled together as a :class:`BackendBundle` for injection into the
application (Requirements 1.4, 5.1, 5.5).

Both backends are assembled with **lazy imports** so that importing this module
never requires backend-specific dependencies (e.g. ``boto3``) or AWS
credentials. The AWS bundle is composed from the ``kiro.backends.aws.*`` modules
(SSM/Secrets Manager/DynamoDB/S3); should any of those modules be unavailable,
requesting the ``aws`` backend raises a clear :class:`NotImplementedError`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from kiro.backends.interfaces import (
    ConfigProvider,
    DebugLogSink,
    SecretProvider,
    StateStore,
    TokenRefreshCoordinator,
)

# Supported backend identifiers.
BACKEND_LOCAL = "local"
BACKEND_AWS = "aws"
SUPPORTED_BACKENDS = (BACKEND_LOCAL, BACKEND_AWS)

# Environment variable that selects the active backend.
STORAGE_BACKEND_ENV = "STORAGE_BACKEND"
DEFAULT_BACKEND = BACKEND_LOCAL


@dataclass
class BackendBundle:
    """
    A complete, consistent set of storage backend implementations.

    Injected into ``AccountManager``, ``KiroAuthManager``, the config loader and
    the debug logger so the rest of the application stays storage-agnostic.
    """

    name: str
    config_provider: ConfigProvider
    secret_provider: SecretProvider
    state_store: StateStore
    coordinator: TokenRefreshCoordinator
    debug_sink: DebugLogSink


def _resolve_backend_name(backend: Optional[str]) -> str:
    """Resolve the backend name from the argument or the environment."""
    name = (backend or os.getenv(STORAGE_BACKEND_ENV, DEFAULT_BACKEND) or DEFAULT_BACKEND).strip().lower()
    if name not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unsupported {STORAGE_BACKEND_ENV}='{name}'. "
            f"Expected one of: {', '.join(SUPPORTED_BACKENDS)}."
        )
    return name


def _create_local_backend() -> BackendBundle:
    """Assemble the LocalFileBackend (development form, behaviourally equivalent to current)."""
    # Imported here to keep import cost local to the selected backend.
    from kiro.backends.local import (
        LocalConfigProvider,
        LocalDebugLogSink,
        LocalSecretProvider,
        LocalStateStore,
        NoopCoordinator,
    )
    from kiro.config import ACCOUNTS_STATE_FILE

    config_provider = LocalConfigProvider()
    secret_provider = LocalSecretProvider(config_provider=config_provider)
    state_store = LocalStateStore(ACCOUNTS_STATE_FILE)
    coordinator = NoopCoordinator()
    debug_sink = LocalDebugLogSink()

    return BackendBundle(
        name=BACKEND_LOCAL,
        config_provider=config_provider,
        secret_provider=secret_provider,
        state_store=state_store,
        coordinator=coordinator,
        debug_sink=debug_sink,
    )


def _resolve_aws_deployment_settings() -> tuple[str, Optional[str], str, str]:
    """
    Resolve AWS deployment-level identifiers from the task environment.

    These identify the AWS resources the gateway binds to and are supplied as
    plain environment variables on the Fargate task (they must be known before
    any SSM/Secrets client can be built, so they are not themselves read from
    SSM).

    Returns:
        ``(stack, region, state_table, debug_bucket)`` where ``region`` may be
        ``None`` (letting ``boto3`` resolve it from the ambient configuration).
    """
    stack = (
        os.getenv("STACK_NAME")
        or os.getenv("GATEWAY_STACK_NAME")
        or "kiro-gateway"
    ).strip()
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    # Mirror DynamoStateStore's own derivation so the state store and the refresh
    # coordinator address exactly the same single table.
    state_table = os.getenv("GATEWAY_STATE_TABLE") or f"{stack}-gateway-state"
    debug_bucket = os.getenv("DEBUG_S3_BUCKET") or f"{stack}-gateway-debug"
    return stack, region, state_table, debug_bucket


def _create_aws_backend() -> BackendBundle:
    """
    Assemble the AwsBackend (cloud-native form).

    All AWS implementations are imported **lazily inside this function** so that
    importing :mod:`kiro.backends.factory` never requires ``boto3`` or AWS
    credentials. The concrete classes live in sibling modules under
    ``kiro.backends.aws`` (delivered across tasks 6.x / 7.x):

    - :class:`kiro.backends.aws.config_provider.SsmConfigProvider` /
      :class:`~kiro.backends.aws.config_provider.S3ConfigProvider` - configuration
      from SSM Parameter Store and ``credentials.json``骨架 from S3.
    - :class:`kiro.backends.aws.secret_provider.SecretsManagerProvider` - secrets
      (PROXY_API_KEY / credentials / tokens) from Secrets Manager.
    - :class:`kiro.backends.aws.state_store.DynamoStateStore` - shared runtime
      state with atomic updates in DynamoDB, wrapped by
      :class:`kiro.backends.cache_layer.CachingStateStore` to add a short-TTL,
      write-through read cache over the account-selection read path
      (Requirements 11.3, 11.5, 12.2).
    - :class:`kiro.backends.aws.coordinator.DynamoRefreshCoordinator` -
      distributed single-flight token refresh via DynamoDB lease locks.
    - :class:`kiro.backends.aws.debug_sink.S3DebugLogSink` - per-request debug
      payload archival to S3.

    Deployment resource names (region, stack prefix, table/bucket names) are
    resolved from the task environment so a single image runs unchanged across
    stacks. See design.md "数据模型" for the resource layout.

    Raises:
        NotImplementedError: If an AWS implementation module is not yet present.
    """
    try:
        from kiro.backends.aws import (  # type: ignore
            DynamoRefreshCoordinator,
            DynamoStateStore,
            S3DebugLogSink,
            SecretsManagerProvider,
            SsmConfigProvider,
        )
    except ImportError as exc:  # pragma: no cover - exercised before AWS siblings land
        raise NotImplementedError(
            "The 'aws' storage backend is not fully implemented yet. "
            "It is assembled from SSM/Secrets Manager/DynamoDB/S3 backends "
            "(kiro.backends.aws.*). Use STORAGE_BACKEND=local until they are available."
        ) from exc

    # Imported here (not at module top) so importing the factory never pulls in
    # the cache layer until the AWS bundle is actually assembled.
    from kiro.backends.cache_layer import CachingStateStore

    stack, region, state_table, debug_bucket = _resolve_aws_deployment_settings()

    # --- Configuration (SSM Parameter Store; credentials.json骨架 via S3/Secrets) ---
    config_provider = SsmConfigProvider(stack, region_name=region)

    # --- Secrets (PROXY_API_KEY / credentials / per-account tokens) ---
    secret_provider = SecretsManagerProvider(stack=stack, region=region)

    # --- Shared runtime state and single-flight token refresh (same DynamoDB table) ---
    # The DynamoDB store is the authoritative cross-instance state; the refresh
    # coordinator must talk to it directly (its lease-lock conditional writes
    # cannot be cached). Account-selection *reads*, however, are fronted by a
    # short-TTL, write-through process cache (STATE_CACHE_TTL_SECONDS, default
    # ~2s) to keep non-streaming read latency low (Requirements 11.3, 11.5) and
    # to reduce billed DynamoDB calls (Requirements 12.2). See
    # design.md "数据模型 → 本地 TTL 缓存层".
    dynamo_state_store = DynamoStateStore(table_name=state_table, region_name=region)
    state_store = CachingStateStore(dynamo_state_store)
    coordinator = DynamoRefreshCoordinator(
        table_name=state_table,
        secret_provider=secret_provider,
        region=region,
    )

    # --- Per-request debug payload archival to S3 ---
    debug_sink = S3DebugLogSink(bucket=debug_bucket, region_name=region)

    return BackendBundle(
        name=BACKEND_AWS,
        config_provider=config_provider,
        secret_provider=secret_provider,
        state_store=state_store,
        coordinator=coordinator,
        debug_sink=debug_sink,
    )


def create_backend(backend: Optional[str] = None) -> BackendBundle:
    """
    Create the storage backend bundle for the requested (or configured) backend.

    Args:
        backend: Explicit backend name (``local`` | ``aws``). When ``None``, the
            value of the ``STORAGE_BACKEND`` environment variable is used,
            defaulting to ``local``.

    Returns:
        A :class:`BackendBundle` with all five backend components assembled.

    Raises:
        ValueError: If the requested backend name is not supported.
        NotImplementedError: If the requested backend is recognised but not yet
            implemented.
    """
    name = _resolve_backend_name(backend)
    logger.info(f"Initializing storage backend: '{name}'")

    if name == BACKEND_LOCAL:
        return _create_local_backend()
    if name == BACKEND_AWS:
        return _create_aws_backend()

    # Unreachable: _resolve_backend_name validates the name.
    raise ValueError(f"Unsupported storage backend: '{name}'")
