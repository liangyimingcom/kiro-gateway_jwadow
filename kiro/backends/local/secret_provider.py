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
LocalSecretProvider - secret provider backed by environment variables / local
files (development form).

Reads sensitive values from the process environment (populated from ``.env``),
supports an in-process write-back for refreshed tokens, and provides log
redaction so that secret values never appear in clear text in log output
(Requirements 5.1, 6.5).
"""

from __future__ import annotations

import json
from typing import Optional

# Mask used to replace secret values in redacted output.
REDACTION_MASK = "***REDACTED***"

# Minimum length of a value before it is registered for redaction. Extremely
# short values (e.g. "1", "") are not treated as secrets to avoid masking
# unrelated substrings throughout the logs.
_MIN_REDACTABLE_LENGTH = 4


class LocalSecretProvider:
    """
    Implements the :class:`SecretProvider` protocol against local sources.

    - ``get_secret`` / ``get_json_secret`` read from environment variables.
    - ``put_secret`` stores values in an in-process override map (development
      write-back) so that a write followed by a read round-trips.
    - ``redact`` masks every known secret value in arbitrary text.
    """

    def __init__(self, config_provider=None) -> None:
        """
        Args:
            config_provider: Optional :class:`ConfigProvider` used to resolve
                secret values. When provided, ``get_secret`` consults it; an
                ``os.getenv`` fallback is always available.
        """
        self._config_provider = config_provider
        # In-process write-back store for refreshed tokens (development form).
        self._overrides: dict[str, str] = {}
        # Registry of known secret values to mask during redaction.
        self._secret_values: set[str] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve(self, name: str) -> Optional[str]:
        """Resolve ``name`` from overrides, then config provider, then env."""
        if name in self._overrides:
            return self._overrides[name]
        if self._config_provider is not None:
            value = self._config_provider.get(name)
            if value is not None:
                return value
        import os

        return os.getenv(name)

    def register_secret(self, value: Optional[str]) -> None:
        """
        Register ``value`` so that subsequent :meth:`redact` calls mask it.

        Values shorter than :data:`_MIN_REDACTABLE_LENGTH` are ignored to avoid
        masking unrelated text.
        """
        if value and len(value) >= _MIN_REDACTABLE_LENGTH:
            self._secret_values.add(value)

    # ------------------------------------------------------------------
    # SecretProvider protocol
    # ------------------------------------------------------------------

    async def get_secret(self, name: str) -> str:
        """
        Return the secret value for ``name``.

        Raises:
            KeyError: If the secret is not defined in any local source.
        """
        value = self._resolve(name)
        if value is None:
            raise KeyError(f"Secret not found: '{name}'")
        # Track the value so it is redacted from any future log output.
        self.register_secret(value)
        return value

    async def get_json_secret(self, name: str) -> dict:
        """Return the secret value for ``name`` parsed as JSON."""
        raw = await self.get_secret(name)
        return json.loads(raw)

    async def put_secret(self, name: str, value: str) -> None:
        """
        Persist ``value`` for ``name``.

        In the local (development) form this writes to an in-process override
        map rather than back to credential files, while still registering the
        value for redaction.
        """
        self._overrides[name] = value
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
        redacted = text
        for secret in sorted(self._secret_values, key=len, reverse=True):
            if secret:
                redacted = redacted.replace(secret, REDACTION_MASK)
        return redacted
