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
S3DebugLogSink - debug log sink that archives per-request debug payloads to
Amazon S3 (Requirements 8.1, 8.2, 12.4).

Each payload is stored at::

    s3://<bucket>/<prefix>/<yyyy>/<mm>/<dd>/<request_id>.json

The date-partitioned key layout lets an S3 lifecycle rule expire archived debug
logs after the operator-configured retention period (Requirements 12.4). The
application's *main* logs continue to flow through loguru -> stdout so they are
collected by CloudWatch Logs (Requirements 8.1); this sink only handles the
per-request debug payload archival that previously wrote to the local
``debug_logs/`` directory (Requirements 8.2).

Design notes:

- The ``boto3`` S3 client is created **lazily** on first write so that importing
  this module never requires ``boto3`` to be installed or AWS credentials to be
  present (the factory imports it lazily; unit/local runs never construct it).
- ``boto3`` is synchronous, so the blocking ``put_object`` call is dispatched to
  the default thread-pool executor to avoid blocking the event loop.
- Errors are handled **non-fatally**: a warning is logged and the exception is
  swallowed so that debug archival can never break the main request path
  (mirroring :class:`~kiro.backends.local.debug_sink.LocalDebugLogSink`).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from loguru import logger

# Default key prefix under which debug payloads are archived.
DEFAULT_DEBUG_PREFIX = "debug"


class S3DebugLogSink:
    """
    Archives per-request debug payloads to Amazon S3.

    Implements the :class:`~kiro.backends.interfaces.DebugLogSink` protocol.

    Args:
        bucket: Name of the S3 bucket used for debug-log archival.
        prefix: Key prefix under which payloads are stored (default ``debug``).
            Leading/trailing slashes are normalised away.
        region_name: Optional AWS region for the S3 client. When ``None`` the
            ambient AWS configuration (env / instance role) is used.
        client: Optional pre-built S3 client. Primarily for testing / dependency
            injection; when provided, ``boto3`` is never imported.
        now_provider: Optional callable returning the current ``datetime`` used
            to build the date partition. Defaults to UTC ``datetime.now``;
            injectable for deterministic tests.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = DEFAULT_DEBUG_PREFIX,
        region_name: Optional[str] = None,
        client: Optional[Any] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3DebugLogSink requires a non-empty bucket name")
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._region_name = region_name
        self._client = client
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Client management (lazy initialisation)
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """
        Return the S3 client, creating it lazily on first use.

        ``boto3`` is imported here (not at module import time) so that this
        module is importable without ``boto3`` installed, and so that no AWS
        client is built unless a debug payload is actually archived.
        """
        if self._client is None:
            import boto3  # Imported lazily; see module docstring.

            self._client = boto3.client("s3", region_name=self._region_name)
        return self._client

    # ------------------------------------------------------------------
    # Key construction
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_request_id(request_id: str) -> str:
        """
        Make ``request_id`` safe to embed in an S3 key.

        Guards against path-traversal / unintended prefixes by replacing path
        separators, exactly as the local sink does for filenames.
        """
        return str(request_id).replace("/", "_").replace("\\", "_")

    def build_key(self, request_id: str) -> str:
        """
        Build the date-partitioned S3 key for ``request_id``.

        Layout: ``<prefix>/<yyyy>/<mm>/<dd>/<request_id>.json``.
        """
        now = self._now_provider()
        safe_request_id = self._sanitize_request_id(request_id)
        date_path = f"{now.year:04d}/{now.month:02d}/{now.day:02d}"
        if self._prefix:
            return f"{self._prefix}/{date_path}/{safe_request_id}.json"
        return f"{date_path}/{safe_request_id}.json"

    # ------------------------------------------------------------------
    # DebugLogSink protocol
    # ------------------------------------------------------------------

    async def write(self, request_id: str, payload: dict) -> None:
        """
        Archive ``payload`` to ``s3://<bucket>/<prefix>/<yyyy>/<mm>/<dd>/<request_id>.json``.

        The blocking ``boto3`` ``put_object`` call runs in the default executor
        so the event loop is not blocked. Any failure is logged as a warning and
        swallowed, so debug archival can never propagate an error into the main
        request path (Requirements 8.2).
        """
        try:
            key = self.build_key(request_id)
            body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            client = self._get_client()

            def _put() -> None:
                client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                )

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _put)
            logger.debug(
                f"[S3DebugLogSink] Debug payload archived to s3://{self._bucket}/{key}"
            )
        except Exception as e:
            # Non-fatal: never break the request path because of debug archival.
            logger.warning(f"[S3DebugLogSink] Failed to archive debug payload: {e}")
