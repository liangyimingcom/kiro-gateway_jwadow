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
SecretsManagerProvider - cloud-native secret provider backed by AWS Secrets
Manager.

Implements the :class:`~kiro.backends.interfaces.SecretProvider` protocol
against AWS Secrets Manager (design.md "组件与接口" 接口 2 + "数据模型"
Secret_Store 布局):

- ``get_secret`` / ``get_json_secret`` read sensitive values
  (``PROXY_API_KEY``, ``credentials.json`` content, per-account tokens) from the
  Secret_Store (Requirements 6.1, 6.2).
- ``put_secret`` writes refreshed tokens back to the Secret_Store rather than to
  local credential files (Requirements 6.7).
- ``redact`` masks every known secret value so credentials never appear in clear
  text in logs (Requirements 6.5).
- A small in-process TTL cache fronts reads to reduce billed API calls
  (design.md 11.5 / 12.2).
- AWS access is via the ambient IAM role / instance credentials; no access keys
  are embedded (Requirements 6.4).

Secret_Store layout (design.md "数据模型"):

    <stack>/proxy-api-key             -> PROXY_API_KEY string
    <stack>/credentials-json          -> full credentials.json content
    <stack>/account/<account_id>/token -> per-account access/refresh token bundle

``boto3`` is imported lazily (inside the client factory) and the client is
created only on first use, so importing this module never requires ``boto3`` to
be installed or AWS credentials to be present.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Optional

# Mask used to replace secret values in redacted output (kept identical to the
# local provider so log output is consistent across backends).
REDACTION_MASK = "***REDACTED***"

# Minimum length of a value before it is registered for redaction. Extremely
# short values (e.g. "1", "") are not treated as secrets to avoid masking
# unrelated substrings throughout the logs.
_MIN_REDACTABLE_LENGTH = 4

# Default TTL (seconds) for the in-process read cache. Reads within the TTL are
# served from memory and do not incur a billed Secrets Manager API call.
_DEFAULT_CACHE_TTL_SECONDS = 300.0

# Environment variables used for zero-argument construction.
_STACK_ENV = "STACK_NAME"
_DEFAULT_STACK = "kiro"
_REGION_ENVS = ("AWS_REGION", "AWS_DEFAULT_REGION")
_CACHE_TTL_ENV = "SECRET_CACHE_TTL_SECONDS"

# Logical names that resolve to the well-known Secret_Store entries regardless of
# the exact casing/separators a caller uses.
_PROXY_API_KEY_ALIASES = frozenset({"PROXY_API_KEY", "proxy-api-key", "proxy_api_key"})
_CREDENTIALS_JSON_ALIASES = frozenset(
    {"credentials.json", "CREDENTIALS_JSON", "credentials-json", "credentials_json"}
)


class SecretsManagerProvider:
    """
    :class:`SecretProvider` implementation backed by AWS Secrets Manager.

    The provider is intentionally tolerant about how secrets are addressed:
    callers may use a *logical* name (e.g. ``"PROXY_API_KEY"`` or
    ``"account/<id>/token"``) or pass a fully-qualified Secret_Store id
    (``"<stack>/proxy-api-key"``). Both resolve to the same underlying secret.
    """

    def __init__(
        self,
        *,
        stack: Optional[str] = None,
        region: Optional[str] = None,
        client=None,
        client_factory=None,
        cache_ttl: Optional[float] = None,
    ) -> None:
        """
        Args:
            stack: Stack/name prefix used to build Secret_Store ids. Defaults to
                the ``STACK_NAME`` environment variable, then ``"kiro"``.
            region: AWS region for the Secrets Manager client. Defaults to
                ``AWS_REGION`` / ``AWS_DEFAULT_REGION``. When ``None`` boto3
                resolves the region from the environment / instance metadata.
            client: An already-constructed Secrets Manager client (primarily for
                tests / dependency injection). When provided it is used as-is and
                ``boto3`` is never imported.
            client_factory: Optional zero-argument callable returning a client.
                Used instead of the default boto3 factory when provided.
            cache_ttl: TTL in seconds for the in-process read cache. Defaults to
                ``SECRET_CACHE_TTL_SECONDS`` then :data:`_DEFAULT_CACHE_TTL_SECONDS`.
                A value ``<= 0`` disables caching.
        """
        self._stack = (stack or os.getenv(_STACK_ENV, _DEFAULT_STACK) or _DEFAULT_STACK).strip().strip("/")
        self._region = region or self._region_from_env()
        self._cache_ttl = self._resolve_cache_ttl(cache_ttl)

        # Lazily-initialised AWS client (created on first use unless injected).
        self._client = client
        self._client_factory = client_factory
        self._client_lock = threading.Lock()

        # In-process TTL read cache: secret_id -> (value, expires_at_monotonic).
        self._cache: dict[str, tuple[str, float]] = {}
        self._cache_lock = threading.Lock()

        # Registry of known secret values to mask during redaction.
        self._secret_values: set[str] = set()
        self._secrets_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _region_from_env() -> Optional[str]:
        for env_name in _REGION_ENVS:
            value = os.getenv(env_name)
            if value:
                return value
        return None

    @staticmethod
    def _resolve_cache_ttl(cache_ttl: Optional[float]) -> float:
        if cache_ttl is not None:
            return float(cache_ttl)
        raw = os.getenv(_CACHE_TTL_ENV)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return _DEFAULT_CACHE_TTL_SECONDS

    # ------------------------------------------------------------------
    # Secret id resolution (Secret_Store layout)
    # ------------------------------------------------------------------

    @property
    def stack(self) -> str:
        """The stack/name prefix used to build Secret_Store ids."""
        return self._stack

    def proxy_api_key_secret_id(self) -> str:
        """Return the Secret_Store id holding ``PROXY_API_KEY``."""
        return f"{self._stack}/proxy-api-key"

    def credentials_json_secret_id(self) -> str:
        """Return the Secret_Store id holding the ``credentials.json`` content."""
        return f"{self._stack}/credentials-json"

    def account_token_secret_id(self, account_id: str) -> str:
        """Return the Secret_Store id holding ``account_id``'s token bundle."""
        return f"{self._stack}/account/{account_id}/token"

    def _resolve_secret_id(self, name: str) -> str:
        """
        Resolve a caller-supplied ``name`` to a fully-qualified Secret_Store id.

        Accepts logical names (``PROXY_API_KEY``, ``credentials.json``,
        ``account/<id>/token``) as well as ids that are already prefixed with the
        stack name (returned unchanged).
        """
        if not name:
            raise KeyError("Secret name must be a non-empty string")

        # Already a fully-qualified id for this stack.
        if name.startswith(f"{self._stack}/"):
            return name

        if name in _PROXY_API_KEY_ALIASES:
            return self.proxy_api_key_secret_id()
        if name in _CREDENTIALS_JSON_ALIASES:
            return self.credentials_json_secret_id()

        # Per-account token addressed as "account/<id>/token".
        if name.startswith("account/") and name.endswith("/token"):
            return f"{self._stack}/{name}"

        # Generic fallback: namespace any other logical key under the stack.
        return f"{self._stack}/{name}"

    # ------------------------------------------------------------------
    # AWS client (lazy, IAM-role based - no embedded access keys)
    # ------------------------------------------------------------------

    def _get_client(self):
        """
        Return the Secrets Manager client, creating it on first use.

        ``boto3`` is imported here (not at module import time) so that importing
        this module never requires the dependency or AWS credentials. The client
        uses the ambient credential chain (IAM role / instance profile); no
        access keys are passed in (Requirements 6.4).
        """
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import boto3  # local import: keeps module importable without boto3

                kwargs = {}
                if self._region:
                    kwargs["region_name"] = self._region
                self._client = boto3.client("secretsmanager", **kwargs)
            return self._client

    # ------------------------------------------------------------------
    # TTL read cache
    # ------------------------------------------------------------------

    def _cache_get(self, secret_id: str) -> Optional[str]:
        if self._cache_ttl <= 0:
            return None
        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(secret_id)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at <= now:
                # Expired - evict and force a refill.
                self._cache.pop(secret_id, None)
                return None
            return value

    def _cache_put(self, secret_id: str, value: str) -> None:
        if self._cache_ttl <= 0:
            return
        with self._cache_lock:
            self._cache[secret_id] = (value, time.monotonic() + self._cache_ttl)

    def invalidate_cache(self, name: Optional[str] = None) -> None:
        """
        Drop cached read(s). With no argument the whole cache is cleared;
        otherwise only the entry for ``name`` (logical or fully-qualified) is
        evicted.
        """
        with self._cache_lock:
            if name is None:
                self._cache.clear()
            else:
                self._cache.pop(self._resolve_secret_id(name), None)

    # ------------------------------------------------------------------
    # Redaction registry
    # ------------------------------------------------------------------

    def register_secret(self, value: Optional[str]) -> None:
        """
        Register ``value`` so subsequent :meth:`redact` calls mask it.

        Values shorter than :data:`_MIN_REDACTABLE_LENGTH` are ignored to avoid
        masking unrelated text. For JSON secret bundles the individual string
        values are also registered so tokens embedded in a larger document are
        masked even when the document itself is not logged verbatim.
        """
        if not value or len(value) < _MIN_REDACTABLE_LENGTH:
            return
        with self._secrets_lock:
            self._secret_values.add(value)
        # Best-effort: also register the leaf string values of a JSON bundle.
        self._register_json_leaves(value)

    def _register_json_leaves(self, raw: str) -> None:
        stripped = raw.strip()
        if not stripped or stripped[0] not in "{[":
            return
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            return

        def _walk(node) -> None:
            if isinstance(node, dict):
                for child in node.values():
                    _walk(child)
            elif isinstance(node, list):
                for child in node:
                    _walk(child)
            elif isinstance(node, str) and len(node) >= _MIN_REDACTABLE_LENGTH:
                with self._secrets_lock:
                    self._secret_values.add(node)

        _walk(parsed)

    # ------------------------------------------------------------------
    # SecretProvider protocol
    # ------------------------------------------------------------------

    async def get_secret(self, name: str) -> str:
        """
        Return the secret value for ``name`` from the Secret_Store.

        Reads are served from the in-process TTL cache when fresh; otherwise a
        single ``GetSecretValue`` call refills the cache. The returned value is
        registered for redaction (Requirements 6.1, 6.2, 6.5).

        Raises:
            KeyError: If the secret does not exist in the Secret_Store.
        """
        secret_id = self._resolve_secret_id(name)

        cached = self._cache_get(secret_id)
        if cached is not None:
            self.register_secret(cached)
            return cached

        value = await asyncio.to_thread(self._fetch_secret_string, secret_id)
        self._cache_put(secret_id, value)
        self.register_secret(value)
        return value

    async def get_json_secret(self, name: str) -> dict:
        """Return the secret value for ``name`` parsed as JSON."""
        raw = await self.get_secret(name)
        return json.loads(raw)

    async def put_secret(self, name: str, value: str) -> None:
        """
        Persist ``value`` for ``name`` in the Secret_Store (e.g. a refreshed
        token), writing back to Secrets Manager rather than a local file
        (Requirements 6.7).

        The cache is updated write-through so an immediate read on this instance
        reflects the new value, and the value is registered for redaction.
        """
        secret_id = self._resolve_secret_id(name)
        await asyncio.to_thread(self._write_secret_string, secret_id, value)
        self._cache_put(secret_id, value)
        self.register_secret(value)

    def redact(self, text: str) -> str:
        """
        Return ``text`` with all known secret values replaced by
        :data:`REDACTION_MASK`.

        Longer secrets are masked first so that a secret which contains another
        registered secret as a substring is fully masked (Requirements 6.5).
        """
        if not text:
            return text
        with self._secrets_lock:
            secrets = sorted(self._secret_values, key=len, reverse=True)
        redacted = text
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, REDACTION_MASK)
        return redacted

    # ------------------------------------------------------------------
    # Blocking AWS calls (run in a worker thread via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _fetch_secret_string(self, secret_id: str) -> str:
        client = self._get_client()
        try:
            response = client.get_secret_value(SecretId=secret_id)
        except Exception as exc:  # noqa: BLE001 - normalise AWS errors
            if self._is_not_found(exc):
                raise KeyError(f"Secret not found: '{secret_id}'") from exc
            raise
        secret_string = response.get("SecretString")
        if secret_string is None:
            # Binary secrets are not used by the gateway; decode defensively.
            binary = response.get("SecretBinary")
            if binary is None:
                raise KeyError(f"Secret has no value: '{secret_id}'")
            if isinstance(binary, bytes):
                return binary.decode("utf-8")
            return str(binary)
        return secret_string

    def _write_secret_string(self, secret_id: str, value: str) -> None:
        client = self._get_client()
        try:
            client.put_secret_value(SecretId=secret_id, SecretString=value)
        except Exception as exc:  # noqa: BLE001
            if self._is_not_found(exc):
                # Secret does not exist yet - create it on first write-back.
                client.create_secret(Name=secret_id, SecretString=value)
            else:
                raise

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        """Detect a Secrets Manager 'not found' error without importing botocore."""
        # botocore ClientError exposes .response['Error']['Code'].
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code")
            if code in ("ResourceNotFoundException", "SecretNotFoundException"):
                return True
        # Fall back to the exception class name (covers stubbed/fake clients).
        return exc.__class__.__name__ in ("ResourceNotFoundException", "SecretNotFoundException")
