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
"""FastAPI application wiring for the remediation orchestrator."""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from app import simulation
from app.config import Settings, get_settings
from app.dashboard import render_dashboard
from app.db import TaskStore
from app.devin_client import build_client
from app.issues import build_issue_source
from app.keyboard import KeyboardTrigger
from app.orchestrator import IssueOutcome, Orchestrator, ScanResult
from app.poller import IssuePoller, PollingWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("main")

router = APIRouter()


def _orchestrator(request: Request) -> Orchestrator:
    orchestrator: Orchestrator = request.app.state.orchestrator
    return orchestrator


def _issue_poller(request: Request) -> IssuePoller:
    poller: IssuePoller = request.app.state.issue_poller
    return poller


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _scan_response(result: ScanResult) -> JSONResponse:
    return JSONResponse(
        {
            "scanned": result.scanned,
            "eligible": result.eligible,
            "triggered": result.triggered,
            "duplicate": result.duplicate,
            "ignored": result.ignored,
            "errors": result.errors,
            "triggered_tasks": [t.model_dump() for t in result.triggered_tasks],
        }
    )


def _simulated(settings: Settings) -> bool:
    return settings.simulation_mode or not settings.devin_configured


def _require_token(request: Request) -> None:
    """Enforce the shared-secret header on state-changing endpoints.

    No-op when ``poll_api_token`` is unset (open by default for the local
    demo); otherwise the request must carry a matching ``X-Auth-Token`` header.
    """
    expected = _settings(request).poll_api_token
    if not expected:
        return
    provided = request.headers.get("x-auth-token", "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Auth-Token")


def _outcome_response(outcome: IssueOutcome) -> JSONResponse:
    return JSONResponse(
        {
            "result": outcome.result,
            "detail": outcome.detail,
            "task": outcome.task.model_dump() if outcome.task else None,
        }
    )


@router.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    return {
        "status": "ok",
        "simulation_mode": _simulated(settings),
        "monitored_repo": settings.github_repo,
        "issue_poll_interval_seconds": settings.issue_poll_interval_seconds,
    }


@router.post("/poll/run")
async def poll_run(request: Request) -> JSONResponse:
    """Manually trigger an immediate scan for eligible issues."""
    _require_token(request)
    result = await _issue_poller(request).scan_now()
    return _scan_response(result)


@router.post("/simulate/issue")
async def simulate_issue(request: Request) -> JSONResponse:
    _require_token(request)
    if not _simulated(_settings(request)):
        raise HTTPException(
            status_code=403, detail="simulation endpoint disabled in live mode"
        )
    issue = simulation.make_issue()
    outcome = await _orchestrator(request).process_issue(issue)
    return _outcome_response(outcome)


@router.get("/metrics")
async def metrics(request: Request) -> JSONResponse:
    return JSONResponse(_orchestrator(request).metrics().model_dump())


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    orch = _orchestrator(request)
    return HTMLResponse(render_dashboard(orch.metrics(), orch.store.list_tasks()))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build dependencies, start the poller, and tidy up on shutdown."""
    settings: Settings = app.state.settings
    store = TaskStore(settings.database_path)
    client = build_client(settings)
    source = build_issue_source(settings)
    orchestrator = Orchestrator(settings, store, client)
    worker = PollingWorker(orchestrator, settings.poll_interval_seconds)
    issue_poller = IssuePoller(
        orchestrator, source, settings.issue_poll_interval_seconds
    )
    keyboard = KeyboardTrigger(issue_poller.scan_now)

    app.state.store = store
    app.state.client = client
    app.state.source = source
    app.state.orchestrator = orchestrator
    app.state.worker = worker
    app.state.issue_poller = issue_poller
    app.state.keyboard = keyboard

    logger.info(
        "Orchestrator starting in %s mode, monitoring %s",
        "SIMULATION" if _simulated(settings) else "LIVE",
        settings.github_repo,
    )
    worker.start()
    if settings.issue_polling_enabled:
        issue_poller.start()
        keyboard.start()
    else:
        logger.info("Issue polling disabled; scan only via /poll/run or 's' key.")
    try:
        yield
    finally:
        await keyboard.stop()
        await issue_poller.stop()
        await worker.stop()
        await source.aclose()
        await client.aclose()
        store.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory (also used by the test-suite)."""
    app = FastAPI(title="Devin Security Remediation Orchestrator", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(router)
    return app


app = create_app()
