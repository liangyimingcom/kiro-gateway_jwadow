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
LocalDebugLogSink - debug log sink that writes to the local ``debug_logs/``
directory (current behaviour, Requirements 8.2).
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from kiro.config import DEBUG_DIR


class LocalDebugLogSink:
    """
    Persists per-request debug payloads to local files under ``DEBUG_DIR``.

    Implements the :class:`DebugLogSink` protocol. Each payload is written as a
    JSON document named after the request id.
    """

    def __init__(self, debug_dir: str = DEBUG_DIR) -> None:
        """
        Args:
            debug_dir: Directory for debug log files (defaults to the configured
                ``DEBUG_DIR``).
        """
        self._debug_dir = Path(debug_dir)

    @property
    def debug_dir(self) -> Path:
        """Directory where per-request debug payloads are written."""
        return self._debug_dir

    async def write(self, request_id: str, payload: dict) -> None:
        """
        Write ``payload`` to ``<debug_dir>/<request_id>.json``.

        Failures are logged but never propagated, so debug logging can never
        break the main request path.
        """
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            # Guard against path traversal / invalid filename characters.
            safe_request_id = str(request_id).replace("/", "_").replace("\\", "_")
            file_path = self._debug_dir / f"{safe_request_id}.json"
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            logger.debug(f"[LocalDebugLogSink] Debug payload saved to {file_path}")
        except Exception as e:
            logger.error(f"[LocalDebugLogSink] Failed to write debug payload: {e}")
