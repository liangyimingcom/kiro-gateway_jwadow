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
AwsBackend - cloud-native storage backend implementations backed by SSM
Parameter Store, Secrets Manager, DynamoDB and S3.

This package is intentionally assembled incrementally across several tasks
(config/secret providers, state store, refresh coordinator, debug sink). To keep
the package importable while siblings are still being delivered - and so that
``import kiro.backends.aws`` never requires AWS credentials or even an installed
``boto3`` - all public classes are exposed via PEP 562 lazy attribute access.

Accessing a name (e.g. ``kiro.backends.aws.SsmConfigProvider``) imports only the
specific submodule that defines it; siblings that do not exist yet are never
touched. The factory's ``build_aws_backend`` symbol is deliberately *not*
exported until the full AWS bundle is complete, so requesting the ``aws`` backend
before then surfaces a clear ``NotImplementedError`` from the factory.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# Map of public class name -> submodule that defines it. Each entry is resolved
# lazily, so this package tolerates siblings that have not been created yet.
_LAZY_EXPORTS = {
    # Task 6.1 - configuration providers
    "SsmConfigProvider": "kiro.backends.aws.config_provider",
    "S3ConfigProvider": "kiro.backends.aws.config_provider",
    # Task 6.2 - secret provider
    "SecretsManagerProvider": "kiro.backends.aws.secret_provider",
    # Task 7.1 - state store
    "DynamoStateStore": "kiro.backends.aws.state_store",
    # Task 7.4 - token refresh coordinator
    "DynamoRefreshCoordinator": "kiro.backends.aws.coordinator",
    # Task 7.6 - debug log sink
    "S3DebugLogSink": "kiro.backends.aws.debug_sink",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazily import and return the requested public attribute (PEP 562)."""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_path)
    return getattr(module, name)


def __dir__() -> list:  # pragma: no cover - introspection helper
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


if TYPE_CHECKING:  # pragma: no cover - import hints for type checkers only
    from kiro.backends.aws.config_provider import (  # noqa: F401
        S3ConfigProvider,
        SsmConfigProvider,
    )
