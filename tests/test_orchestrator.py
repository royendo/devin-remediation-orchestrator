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
"""Unit and integration tests for the orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import github
from app.config import Settings
from app.db import TaskStore
from app.devin_client import SimulatedDevinClient
from app.main import create_app
from app.models import GitHubIssue, TaskStatus
from app.orchestrator import Orchestrator


def _issue(labels: list[str], number: int = 1) -> GitHubIssue:
    return GitHubIssue(
        number=number,
        title="Bump dependency",
        body="please fix",
        html_url=f"https://github.com/o/r/issues/{number}",
        repo_full_name="o/r",
        labels=labels,
    )


def _orchestrator(duration: float = 0.0) -> Orchestrator:
    settings = Settings(simulation_mode=True, database_path=":memory:")
    store = TaskStore(":memory:")
    client = SimulatedDevinClient(duration=duration, failure_rate=0.0)
    return Orchestrator(settings, store, client)


def test_signature_verification() -> None:
    secret = "s3cr3t"
    body = b'{"hello":"world"}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert github.verify_signature(secret, body, sig)
    assert not github.verify_signature(secret, body, "sha256=deadbeef")
    # No configured secret => skipped (dev mode).
    assert github.verify_signature("", body, None)


def test_label_eligibility() -> None:
    triggers = {"security", "dependency", "code-quality", "devin-remediate"}
    assert github.is_eligible("opened", _issue(["security"]), triggers)
    assert not github.is_eligible("opened", _issue(["question"]), triggers)
    assert not github.is_eligible("edited", _issue(["security"]), triggers)


def test_process_issue_triggers_and_dedupes() -> None:
    orch = _orchestrator()

    outcome = asyncio.run(orch.process_issue("opened", _issue(["security"])))
    assert outcome.result == "triggered"
    assert outcome.task is not None
    assert outcome.task.status == TaskStatus.RUNNING
    assert outcome.task.session_id is not None

    dup = asyncio.run(orch.process_issue("opened", _issue(["security"])))
    assert dup.result == "duplicate"

    ignored = asyncio.run(orch.process_issue("opened", _issue(["docs"], number=2)))
    assert ignored.result == "ignored"


def test_poller_marks_completed() -> None:
    orch = _orchestrator(duration=0.0)
    asyncio.run(orch.process_issue("opened", _issue(["dependency"])))
    updated = asyncio.run(orch.refresh_active())
    assert updated == 1

    tasks = orch.store.list_tasks()
    assert tasks[0].status == TaskStatus.COMPLETED
    assert tasks[0].pr_url is not None
    assert tasks[0].completed_at is not None

    metrics = orch.metrics()
    assert metrics.completed_sessions == 1
    assert metrics.prs_created == 1
    assert metrics.success_rate == 1.0


def test_poller_marks_failed() -> None:
    settings = Settings(simulation_mode=True, database_path=":memory:")
    store = TaskStore(":memory:")
    client = SimulatedDevinClient(duration=0.0, failure_rate=1.0)
    orch = Orchestrator(settings, store, client)

    asyncio.run(orch.process_issue("opened", _issue(["security"])))
    asyncio.run(orch.refresh_active())

    task = store.list_tasks()[0]
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert orch.metrics().failed_sessions == 1


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(simulation_mode=True, database_path=":memory:")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_webhook_flow(client: TestClient) -> None:
    payload = {
        "action": "opened",
        "issue": {
            "number": 7,
            "title": "XSS in template",
            "body": "fix it",
            "html_url": "https://github.com/o/r/issues/7",
            "labels": [{"name": "security"}],
        },
        "repository": {"full_name": "o/r"},
    }
    resp = client.post(
        "/webhooks/github",
        content=json.dumps(payload),
        headers={"X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "triggered"

    metrics = client.get("/metrics").json()
    assert metrics["total"] == 1

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Remediation Orchestrator" in dashboard.text


def test_webhook_ignores_non_issue_events(client: TestClient) -> None:
    resp = client.post(
        "/webhooks/github",
        content="{}",
        headers={"X-GitHub-Event": "push"},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "ignored"


def test_simulate_endpoint(client: TestClient) -> None:
    resp = client.post("/simulate/issue")
    assert resp.status_code == 200
    assert resp.json()["result"] == "triggered"
