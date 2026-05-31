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
"""Background worker that reconciles active Devin sessions."""

from __future__ import annotations

import asyncio
import logging

from app.orchestrator import Orchestrator

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
