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
Storage Backend Abstraction Layer for Kiro Gateway.

This package defines storage-agnostic interfaces (``Protocol``) and shared data
types that decouple the application from concrete storage implementations.

Two implementation families are provided:

- ``local``  : LocalFileBackend - behaviourally equivalent to the current
               single-host gateway (``.env`` / ``credentials.json`` /
               ``state.json`` / local credential files / ``debug_logs/``).
- ``aws``    : AwsBackend - cloud-native implementation backed by SSM Parameter
               Store, Secrets Manager, DynamoDB and S3 (implemented in later
               tasks).

The active backend is selected at startup via the ``STORAGE_BACKEND``
environment variable (``local`` | ``aws``) through :mod:`kiro.backends.factory`.
"""

from kiro.backends.interfaces import (
    AccountState,
    AccountStatsState,
    ConfigProvider,
    DebugLogSink,
    MissingConfigError,
    SecretProvider,
    StateStore,
    TokenBundle,
    TokenRefreshCoordinator,
)

__all__ = [
    # Interfaces
    "ConfigProvider",
    "SecretProvider",
    "StateStore",
    "TokenRefreshCoordinator",
    "DebugLogSink",
    # Shared data types
    "AccountState",
    "AccountStatsState",
    "TokenBundle",
    # Exceptions
    "MissingConfigError",
]
