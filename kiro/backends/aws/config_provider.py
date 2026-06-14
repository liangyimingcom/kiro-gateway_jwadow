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
AWS configuration providers.

This module implements the cloud-native side of :class:`ConfigProvider`
(see design.md "组件与接口" - interface 1 and "数据模型" - Config_Store layout):

- :class:`SsmConfigProvider` reads non-sensitive configuration from AWS Systems
  Manager (SSM) Parameter Store, batch-fetching every parameter under the prefix
  ``/<stack>/config`` via ``GetParametersByPath`` and applying the configuration
  precedence ``SSM explicit value > environment variable > code default``
  (Requirements 5.1, 5.3).

- :class:`S3ConfigProvider` reads the multi-account ``credentials.json`` skeleton
  from ``s3://<bucket>/config/credentials.json`` (Requirements 5.2).

Both providers create their AWS clients lazily, so importing this module - and
even constructing a provider - never requires AWS credentials, an installed
``boto3`` or network access. Clients are only created on the first read.

When a required item is absent from both SSM and S3, the providers log a clear,
actionable error and raise :class:`MissingConfigError` (which carries the missing
key name). Raising at start-up lets the application exit with a non-zero status
so the ECS task fails fast rather than serving with missing configuration
(Requirements 5.4).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from loguru import logger

from kiro.backends.interfaces import MissingConfigError

# Default S3 object key for the multi-account credentials skeleton.
DEFAULT_CREDENTIALS_KEY = "config/credentials.json"


def _normalise_prefix(prefix: str) -> str:
    """Return ``prefix`` without a trailing slash (but keeping a single leading slash)."""
    prefix = prefix.strip()
    if len(prefix) > 1:
        prefix = prefix.rstrip("/")
    return prefix


# ==================================================================================================
# SsmConfigProvider
# ==================================================================================================


class SsmConfigProvider:
    """
    Configuration provider backed by AWS SSM Parameter Store.

    Implements the :class:`kiro.backends.interfaces.ConfigProvider` protocol.

    Parameters are stored under the path prefix ``/<stack>/config`` (for example
    ``/<stack>/config/STREAMING_READ_TIMEOUT`` or
    ``/<stack>/config/model-aliases/<alias>``). On :meth:`reload` all parameters
    under that prefix are fetched in batches with ``GetParametersByPath``
    (``Recursive=True``) and cached in-process, keyed by the portion of the path
    that follows the prefix (e.g. ``STREAMING_READ_TIMEOUT`` or
    ``model-aliases/<alias>``).

    Configuration precedence (highest first): SSM explicit value > environment
    variable > code default (Requirements 5.3).
    """

    def __init__(
        self,
        stack: str,
        *,
        prefix: Optional[str] = None,
        region_name: Optional[str] = None,
        with_decryption: bool = True,
        client: Optional[Any] = None,
    ) -> None:
        """
        Args:
            stack: Deployment stack name used to derive the parameter path prefix.
            prefix: Explicit path prefix overriding ``/<stack>/config``.
            region_name: AWS region for the SSM client. When ``None``, boto3
                resolves the region from the standard environment / config chain.
            with_decryption: Whether to request decryption of ``SecureString``
                parameters (harmless for ``String`` parameters).
            client: Pre-built SSM client (mainly for testing / dependency
                injection). When provided, ``region_name`` is ignored.
        """
        self._stack = stack
        self._prefix = _normalise_prefix(prefix if prefix is not None else f"/{stack}/config")
        self._region_name = region_name
        self._with_decryption = with_decryption
        self._client = client
        # ``None`` means "not loaded yet"; an empty dict is a valid loaded state.
        self._cache: Optional[dict] = None

    # -- AWS client (lazy) -------------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return the SSM client, creating it lazily on first use."""
        if self._client is None:
            import boto3  # imported lazily so module import never needs boto3

            self._client = boto3.client("ssm", region_name=self._region_name)
        return self._client

    def _ensure_loaded(self) -> None:
        """Load parameters from SSM on first access."""
        if self._cache is None:
            self.reload()

    # -- ConfigProvider protocol -------------------------------------------------------------------

    def reload(self) -> None:
        """
        (Re)load all parameters under the configured prefix from SSM Parameter
        Store and refresh the in-process cache.

        Effective after a restart / redeployment (Requirements 5.6).

        Raises:
            Exception: Propagates client errors so that start-up fails fast and
                visibly rather than serving with an empty configuration.
        """
        client = self._get_client()
        cache: dict = {}
        prefix_with_slash = self._prefix.rstrip("/") + "/"
        try:
            paginator = client.get_paginator("get_parameters_by_path")
            pages = paginator.paginate(
                Path=self._prefix,
                Recursive=True,
                WithDecryption=self._with_decryption,
            )
            for page in pages:
                for param in page.get("Parameters", []):
                    name = param.get("Name", "")
                    if name.startswith(prefix_with_slash):
                        short_key = name[len(prefix_with_slash):]
                    else:
                        # Defensive: keep the trailing path segment if the prefix
                        # does not match exactly.
                        short_key = name.rsplit("/", 1)[-1]
                    cache[short_key] = param.get("Value", "")
        except Exception as exc:  # noqa: BLE001 - re-raised after logging
            logger.error(
                f"Failed to load configuration from SSM Parameter Store under "
                f"'{self._prefix}': {type(exc).__name__}: {exc}"
            )
            raise
        self._cache = cache
        logger.info(
            f"Loaded {len(cache)} configuration parameter(s) from SSM under '{self._prefix}'."
        )

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Return the value for ``key`` applying the configuration precedence
        ``SSM explicit value > environment variable > code default``.
        """
        self._ensure_loaded()
        assert self._cache is not None  # for type checkers; set by _ensure_loaded
        if key in self._cache:
            return self._cache[key]
        env_value = os.getenv(key)
        if env_value is not None:
            return env_value
        return default

    def get_required(self, key: str) -> str:
        """
        Return the value for ``key`` from SSM or the environment.

        Raises:
            MissingConfigError: If ``key`` is absent from both SSM and the
                environment. A clear error is logged first so operators can
                diagnose the start-up failure (Requirements 5.4).
        """
        # Sentinel distinguishes "missing" from a legitimately empty string value.
        sentinel = object()
        value = self.get(key, default=sentinel)  # type: ignore[arg-type]
        if value is sentinel:
            logger.error(
                f"Required configuration key '{key}' is missing from SSM Parameter "
                f"Store (prefix '{self._prefix}') and the environment."
            )
            raise MissingConfigError(key)
        return value  # type: ignore[return-value]

    def get_namespace(self, prefix: str) -> dict:
        """
        Return all key/value pairs whose key starts with ``prefix``.

        Used for batch reads such as model alias / mapping tables. SSM-sourced
        values take precedence over equally-named environment variables.
        """
        self._ensure_loaded()
        assert self._cache is not None
        result: dict = {
            key: value for key, value in os.environ.items() if key.startswith(prefix)
        }
        # Overlay SSM values so they win over environment variables (Requirements 5.3).
        for key, value in self._cache.items():
            if key.startswith(prefix):
                result[key] = value
        return result


# ==================================================================================================
# S3ConfigProvider
# ==================================================================================================


class S3ConfigProvider:
    """
    Reads the multi-account ``credentials.json`` skeleton from S3.

    The ``credentials.json`` file is too large and structured to be a single SSM
    parameter, so its non-sensitive skeleton lives at
    ``s3://<bucket>/config/credentials.json`` (Requirements 5.2). Sensitive
    fields (e.g. refresh tokens) are merged in from Secrets Manager by the secret
    provider in a separate task; this provider only loads the skeleton.

    The S3 client is created lazily, so importing this module and constructing the
    provider never require AWS credentials or network access.
    """

    def __init__(
        self,
        bucket: str,
        *,
        key: str = DEFAULT_CREDENTIALS_KEY,
        region_name: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        """
        Args:
            bucket: S3 bucket holding the configuration objects.
            key: Object key of the credentials skeleton. Defaults to
                ``config/credentials.json``.
            region_name: AWS region for the S3 client. When ``None``, boto3
                resolves the region from the standard environment / config chain.
            client: Pre-built S3 client (mainly for testing / dependency
                injection). When provided, ``region_name`` is ignored.
        """
        self._bucket = bucket
        self._key = key
        self._region_name = region_name
        self._client = client
        self._cache: Optional[list] = None

    # -- AWS client (lazy) -------------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return the S3 client, creating it lazily on first use."""
        if self._client is None:
            import boto3  # imported lazily so module import never needs boto3

            self._client = boto3.client("s3", region_name=self._region_name)
        return self._client

    @property
    def uri(self) -> str:
        """Return the ``s3://<bucket>/<key>`` URI of the credentials skeleton."""
        return f"s3://{self._bucket}/{self._key}"

    # -- Loading -----------------------------------------------------------------------------------

    def _is_missing_object_error(self, exc: Exception) -> bool:
        """Return ``True`` if ``exc`` indicates the object/bucket does not exist."""
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        code = str(response.get("Error", {}).get("Code", ""))
        return code in {"NoSuchKey", "NoSuchBucket", "404", "AccessDenied"}

    def reload(self) -> None:
        """Clear the cached credentials so the next read re-fetches from S3."""
        self._cache = None

    def get_credentials(self) -> list:
        """
        Load and return the multi-account credentials skeleton as a list of
        account entries (parsed from JSON), caching the result in-process.

        Returns:
            The parsed ``credentials.json`` content (a list of account dicts).

        Raises:
            MissingConfigError: If the object does not exist in S3 - the
                credentials skeleton is required configuration, so a clear error
                is logged and start-up is aborted (Requirements 5.2, 5.4).
            ValueError: If the object exists but does not contain a JSON list.
        """
        if self._cache is not None:
            return self._cache

        client = self._get_client()
        try:
            response = client.get_object(Bucket=self._bucket, Key=self._key)
            body = response["Body"].read()
        except Exception as exc:  # noqa: BLE001 - inspected then re-raised/translated
            if self._is_missing_object_error(exc):
                logger.error(
                    f"Required credentials skeleton not found at '{self.uri}'. "
                    f"The multi-account configuration is mandatory; aborting start-up."
                )
                raise MissingConfigError(
                    self._key,
                    message=(
                        f"Required configuration object is missing: '{self.uri}'"
                    ),
                ) from exc
            logger.error(
                f"Failed to read credentials skeleton from '{self.uri}': "
                f"{type(exc).__name__}: {exc}"
            )
            raise

        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.error(f"credentials.json at '{self.uri}' is not valid JSON: {exc}")
            raise ValueError(f"Invalid JSON in '{self.uri}': {exc}") from exc

        if not isinstance(data, list):
            logger.error(
                f"credentials.json at '{self.uri}' must contain a JSON array of "
                f"account entries, got {type(data).__name__}."
            )
            raise ValueError(
                f"credentials.json at '{self.uri}' must be a JSON array, "
                f"got {type(data).__name__}."
            )

        self._cache = data
        logger.info(f"Loaded {len(data)} credential entr(ies) from '{self.uri}'.")
        return data

    def get_required_credentials(self) -> list:
        """
        Return the credentials skeleton, additionally requiring it to be
        non-empty.

        Raises:
            MissingConfigError: If the skeleton is missing or empty - at least one
                account entry is required for the gateway to operate
                (Requirements 5.2, 5.4).
        """
        data = self.get_credentials()
        if not data:
            logger.error(
                f"credentials skeleton at '{self.uri}' is empty; at least one "
                f"account entry is required. Aborting start-up."
            )
            raise MissingConfigError(
                self._key,
                message=f"Credentials skeleton at '{self.uri}' is empty.",
            )
        return data
