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

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import github, simulation
from app.config import Settings, get_settings
from app.dashboard import render_dashboard
from app.db import TaskStore
from app.devin_client import build_client
from app.orchestrator import IssueOutcome, Orchestrator
from app.poller import PollingWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("main")

router = APIRouter()


def _orchestrator(request: Request) -> Orchestrator:
    orchestrator: Orchestrator = request.app.state.orchestrator
    return orchestrator


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _simulated(settings: Settings) -> bool:
    return settings.simulation_mode or not settings.devin_configured


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
    return {"status": "ok", "simulation_mode": _simulated(_settings(request))}


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> JSONResponse:
    settings = _settings(request)
    body = await request.body()
    if not github.verify_signature(
        settings.github_webhook_secret, body, x_hub_signature_256
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event == "ping":
        return JSONResponse({"result": "pong"})
    if x_github_event != "issues":
        return JSONResponse({"result": "ignored", "detail": "not an issues event"})

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    action, issue = github.parse_issue_payload(payload)
    if issue is None:
        return JSONResponse({"result": "ignored", "detail": "no issue in payload"})

    outcome = await _orchestrator(request).process_issue(action, issue)
    return _outcome_response(outcome)


@router.post("/simulate/issue")
async def simulate_issue(request: Request) -> JSONResponse:
    if not _simulated(_settings(request)):
        raise HTTPException(
            status_code=403, detail="simulation endpoint disabled in live mode"
        )
    issue = simulation.make_issue()
    outcome = await _orchestrator(request).process_issue("opened", issue)
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
    orchestrator = Orchestrator(settings, store, client)
    worker = PollingWorker(orchestrator, settings.poll_interval_seconds)

    app.state.store = store
    app.state.client = client
    app.state.orchestrator = orchestrator
    app.state.worker = worker

    logger.info(
        "Orchestrator starting in %s mode",
        "SIMULATION" if _simulated(settings) else "LIVE",
    )
    worker.start()
    try:
        yield
    finally:
        await worker.stop()
        await client.aclose()
        store.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory (also used by the test-suite)."""
    app = FastAPI(title="Devin Security Remediation Orchestrator", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(router)
    return app


app = create_app()
