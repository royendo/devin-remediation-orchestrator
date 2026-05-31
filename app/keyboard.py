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
"""Interactive keyboard shortcut to trigger an immediate issue scan.

When the server runs attached to a terminal, pressing the configured key (``s``
by default) forces the monitor to scan the repository right away instead of
waiting for the next interval.  Falls back to a no-op when stdin is not a TTY
(e.g. under Docker without ``-it`` or during tests).
"""

from __future__ import annotations

import asyncio
import logging
import select
import sys
from collections.abc import Awaitable, Callable

logger = logging.getLogger("keyboard")

# Read with a short timeout so the loop can notice ``stop`` promptly.
_SELECT_TIMEOUT = 0.5


class KeyboardTrigger:
    """Listens for a single keypress on stdin and runs a callback."""

    def __init__(
        self,
        on_trigger: Callable[[], Awaitable[object]],
        key: str = "s",
    ) -> None:
        self._on_trigger = on_trigger
        self._key = key.lower()
        self._task: asyncio.Task[None] | None = None
        self._stop = False

    def start(self) -> None:
        """Begin listening, if attached to an interactive terminal."""
        if not sys.stdin or not sys.stdin.isatty():
            logger.info(
                "Keyboard shortcut disabled (stdin is not a TTY); "
                "use POST /poll/run to trigger a scan."
            )
            return
        try:
            import termios  # noqa: F401  (probe for POSIX terminal support)
        except ImportError:
            logger.info("Keyboard shortcut unavailable on this platform.")
            return
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="keyboard-trigger")

    async def stop(self) -> None:
        """Stop listening and restore the terminal."""
        self._stop = True
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:  # pragma: no cover - defensive
                pass
            self._task = None

    async def _run(self) -> None:
        import termios
        import tty

        loop = asyncio.get_running_loop()
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            logger.info(
                "Keyboard shortcut active: press '%s' to scan now (Ctrl-C to quit).",
                self._key,
            )
            while not self._stop:
                ready: tuple[list[object], list[object], list[object]] = (
                    await loop.run_in_executor(
                        None, select.select, [sys.stdin], [], [], _SELECT_TIMEOUT
                    )
                )
                if self._stop:
                    break
                if not ready[0]:
                    continue
                char = sys.stdin.read(1)
                if char and char.lower() == self._key:
                    logger.info("Manual scan triggered via keyboard.")
                    try:
                        await self._on_trigger()
                    except Exception:  # noqa: BLE001 - never kill the listener
                        logger.exception("Keyboard-triggered scan failed")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
