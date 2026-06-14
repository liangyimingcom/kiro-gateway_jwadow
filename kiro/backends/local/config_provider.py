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
LocalConfigProvider - configuration provider backed by ``.env`` / environment
variables, preserving the gateway's existing ``python-dotenv`` loading logic and
Windows path handling.

Precedence (highest first): environment variable > code default.
(The full SSM > env > default precedence is realised by the AWS provider in a
later task; the local provider implements the env > default tail.)
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv

from kiro.backends.interfaces import MissingConfigError
# Reuse the existing raw .env reader so Windows paths (with backslashes) are not
# mangled by escape-sequence interpretation, exactly as kiro/config.py does.
from kiro.config import _get_raw_env_value


class LocalConfigProvider:
    """
    Reads configuration from the process environment (populated from ``.env`` via
    ``python-dotenv``), implementing the :class:`ConfigProvider` protocol.
    """

    def __init__(self, env_file: str = ".env") -> None:
        """
        Args:
            env_file: Path to the ``.env`` file to load. Defaults to ``.env`` in
                the current working directory (matching current behaviour).
        """
        self._env_file = env_file
        self.reload()

    def reload(self) -> None:
        """
        (Re)load the ``.env`` file into the process environment.

        ``override=False`` preserves any variables already set in the real
        environment, keeping the precedence "real env var > .env file value".
        """
        load_dotenv(self._env_file, override=False)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Return the value for ``key`` from the environment, or ``default`` if it
        is not set.

        Precedence: environment variable (incl. values loaded from ``.env``) >
        provided default.
        """
        value = os.getenv(key)
        if value is None:
            return default
        return value

    def get_required(self, key: str) -> str:
        """
        Return the value for ``key``.

        Raises:
            MissingConfigError: If ``key`` is not present in the environment.
        """
        value = os.getenv(key)
        if value is None:
            raise MissingConfigError(key)
        return value

    def get_namespace(self, prefix: str) -> dict[str, str]:
        """
        Return all environment key/value pairs whose key starts with ``prefix``.

        Used for batch reads such as model alias / mapping tables.
        """
        return {
            key: value
            for key, value in os.environ.items()
            if key.startswith(prefix)
        }

    def get_raw(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Return the raw, uninterpreted value of ``key`` directly from the ``.env``
        file (without escape-sequence processing).

        This is needed for Windows file-system paths (e.g. ``D:\\Projects\\f.json``)
        where backslash sequences would otherwise be misinterpreted. Falls back
        to the regular environment value and then ``default``.
        """
        raw = _get_raw_env_value(key, self._env_file)
        if raw is not None:
            return raw
        return self.get(key, default)
