# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Background workers: session reconciliation and repository issue polling."""

from __future__ import annotations

import asyncio
import logging

from app.issues import IssueSourceProtocol
from app.orchestrator import Orchestrator, ScanResult

logger = logging.getLogger("poller")


class PollingWorker:
    """Periodically asks the orchestrator to refresh active sessions."""

    def __init__(self, orchestrator: Orchestrator, interval_seconds: float) -> None:
        self._orchestrator = orchestrator
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """Launch the polling loop as a background task."""
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="devin-poller")
            logger.info("Polling worker started (interval=%ss)", self._interval)

    async def stop(self) -> None:
        """Signal the loop to stop and wait for it to finish."""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                updated = await self._orchestrator.refresh_active()
                if updated:
                    logger.info("Reconciled %s session(s)", updated)
            except Exception:  # noqa: BLE001 - keep the loop alive on errors
                logger.exception("Polling cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue


class IssuePoller:
    """Periodically scans a repository for new eligible issues.

    This is the standalone monitor: it pulls open issues from an
    :class:`~app.issues.IssueSourceProtocol` on a fixed interval and hands new
    ones to the orchestrator.  :meth:`scan_now` runs the same scan on demand
    (used by the ``/poll/run`` endpoint and the keyboard shortcut); a lock
    ensures the scheduled loop and a manual trigger never overlap.
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        source: IssueSourceProtocol,
        interval_seconds: float,
    ) -> None:
        self._orchestrator = orchestrator
        self._source = source
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    def start(self) -> None:
        """Launch the issue-scanning loop as a background task."""
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="devin-issue-poller")
            logger.info(
                "Issue poller started (interval=%ss)",
                self._interval,
            )

    async def stop(self) -> None:
        """Signal the loop to stop and wait for it to finish."""
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def scan_now(self) -> ScanResult:
        """Run a single scan immediately, serialised against the loop."""
        async with self._lock:
            return await self._orchestrator.scan_open_issues(self._source)

    async def _run(self) -> None:
        # Scan once on startup so the monitor reacts immediately.
        await self._safe_scan()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                await self._safe_scan()

    async def _safe_scan(self) -> None:
        try:
            await self.scan_now()
        except Exception:  # noqa: BLE001 - keep the loop alive on errors
            logger.exception("Issue scan failed")
