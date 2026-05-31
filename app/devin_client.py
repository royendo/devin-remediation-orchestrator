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
"""Clients for the Devin API.

``DevinClient`` talks to the real ``/v3`` API.  ``SimulatedDevinClient`` mimics
the same interface deterministically so the whole pipeline can be exercised
with no network access or credentials.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.config import Settings


@dataclass
class SessionResult:
    """Normalised view of a Devin session."""

    session_id: str
    session_url: str
    status: str
    pr_url: str | None = None
    error: str | None = None


class DevinClientProtocol(Protocol):
    """Interface implemented by both the real and simulated clients."""

    async def create_session(self, prompt: str) -> SessionResult: ...

    async def get_session(self, session_id: str) -> SessionResult: ...

    async def aclose(self) -> None: ...


def _extract_pr_url(payload: dict[str, object]) -> str | None:
    """Best-effort extraction of a PR URL from a Devin session payload.

    The exact field has shifted across API versions, so we probe the handful
    of shapes that have been observed rather than assuming a single key.
    """
    candidates: list[object] = [
        payload.get("pull_request"),
        payload.get("pull_request_url"),
        payload.get("pr_url"),
        payload.get("structured_output"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
        if isinstance(candidate, dict):
            for key in ("url", "html_url", "pr_url", "pull_request_url"):
                value = candidate.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    return value
    return None


class DevinClient:
    """Thin async wrapper over the Devin ``/v3`` sessions API."""

    def __init__(self, settings: Settings) -> None:
        self._org_id = settings.devin_org_id
        self._base = settings.devin_api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {settings.devin_api_key}"},
            timeout=httpx.Timeout(30.0),
        )

    async def create_session(self, prompt: str) -> SessionResult:
        resp = await self._client.post(
            f"/organizations/{self._org_id}/sessions",
            json={"prompt": prompt, "idempotent": True},
        )
        resp.raise_for_status()
        data = resp.json()
        return SessionResult(
            session_id=data["session_id"],
            session_url=data.get(
                "url", f"https://app.devin.ai/sessions/{data['session_id']}"
            ),
            status=str(data.get("status", "running")),
            pr_url=_extract_pr_url(data),
        )

    async def get_session(self, session_id: str) -> SessionResult:
        resp = await self._client.get(
            f"/organizations/{self._org_id}/sessions/{session_id}"
        )
        resp.raise_for_status()
        data = resp.json()
        return SessionResult(
            session_id=session_id,
            session_url=data.get("url", f"https://app.devin.ai/sessions/{session_id}"),
            status=str(data.get("status", "running")),
            pr_url=_extract_pr_url(data),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass
class _SimSession:
    session_id: str
    created_at: float
    will_fail: bool


@dataclass
class SimulatedDevinClient:
    """Deterministic, offline stand-in for :class:`DevinClient`.

    A session is reported as ``running`` until ``duration`` seconds have elapsed
    since creation, after which it transitions to ``finished`` (with a fake PR
    URL) or ``error`` based on a hash of its id, so behaviour is reproducible.
    """

    duration: float = 20.0
    failure_rate: float = 0.2
    _sessions: dict[str, _SimSession] = field(default_factory=dict)
    _counter: int = 0

    async def create_session(self, prompt: str) -> SessionResult:
        self._counter += 1
        session_id = f"devin-sim-{self._counter:04d}"
        digest = hashlib.sha256(session_id.encode()).digest()
        # Map the first byte into [0, 1) to decide pass/fail deterministically.
        will_fail = (digest[0] / 255.0) < self.failure_rate
        self._sessions[session_id] = _SimSession(
            session_id=session_id,
            created_at=time.monotonic(),
            will_fail=will_fail,
        )
        return SessionResult(
            session_id=session_id,
            session_url=f"https://app.devin.ai/sessions/{session_id}",
            status="running",
        )

    async def get_session(self, session_id: str) -> SessionResult:
        sim = self._sessions.get(session_id)
        url = f"https://app.devin.ai/sessions/{session_id}"
        if sim is None:
            return SessionResult(session_id, url, "error", error="unknown session")
        elapsed = time.monotonic() - sim.created_at
        if elapsed < self.duration:
            return SessionResult(session_id, url, "running")
        if sim.will_fail:
            return SessionResult(
                session_id,
                url,
                "error",
                error="Simulated remediation failure",
            )
        number = (int(session_id.split("-")[-1]) % 9000) + 1000
        pr_url = f"https://github.com/example/repo/pull/{number}"
        return SessionResult(session_id, url, "finished", pr_url=pr_url)

    async def aclose(self) -> None:
        return None


def build_client(settings: Settings) -> DevinClientProtocol:
    """Return the appropriate client for the current configuration."""
    if settings.simulation_mode or not settings.devin_configured:
        return SimulatedDevinClient(
            duration=settings.sim_session_duration_seconds,
            failure_rate=settings.sim_failure_rate,
        )
    return DevinClient(settings)
