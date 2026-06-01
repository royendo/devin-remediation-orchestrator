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
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import github
from app.config import Settings
from app.db import TaskStore
from app.devin_client import SimulatedDevinClient
from app.issues import IssueSourceProtocol, SimulatedIssueSource
from app.main import create_app
from app.models import GitHubIssue, TaskStatus
from app.orchestrator import Orchestrator


class _StaticIssueSource:
    """Test issue source returning a fixed list."""

    def __init__(self, issues: list[GitHubIssue]) -> None:
        self._issues = issues

    async def list_open_issues(self) -> list[GitHubIssue]:
        return list(self._issues)

    async def aclose(self) -> None:
        return None


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


def test_has_trigger_label() -> None:
    triggers = {"security", "dependency", "code-quality", "devin-remediate"}
    assert github.has_trigger_label(_issue(["SECURITY"]), triggers)
    assert not github.has_trigger_label(_issue(["question"]), triggers)


def test_scan_open_issues_triggers_and_dedupes() -> None:
    orch = _orchestrator()
    source = _StaticIssueSource(
        [
            _issue(["security"], number=1),
            _issue(["dependency"], number=2),
            _issue(["question"], number=3),  # ineligible
        ]
    )

    first = asyncio.run(orch.scan_open_issues(source))
    assert first.scanned == 3
    assert first.eligible == 2
    assert first.triggered == 2
    assert len(first.triggered_tasks) == 2

    # A second scan over the same issues must not start new sessions.
    second = asyncio.run(orch.scan_open_issues(source))
    assert second.triggered == 0
    assert second.duplicate == 2
    assert len(orch.store.list_tasks()) == 2


def test_simulated_issue_source_returns_eligible_issues() -> None:
    source: IssueSourceProtocol = SimulatedIssueSource(repo="o/r")
    issues = asyncio.run(source.list_open_issues())
    assert len(issues) >= 5
    assert all(issue.repo_full_name == "o/r" for issue in issues)


def test_process_issue_triggers_and_dedupes() -> None:
    orch = _orchestrator()

    outcome = asyncio.run(orch.process_issue(_issue(["security"])))
    assert outcome.result == "triggered"
    assert outcome.task is not None
    assert outcome.task.status == TaskStatus.RUNNING
    assert outcome.task.session_id is not None

    dup = asyncio.run(orch.process_issue(_issue(["security"])))
    assert dup.result == "duplicate"

    ignored = asyncio.run(orch.process_issue(_issue(["docs"], number=2)))
    assert ignored.result == "ignored"


def test_poller_marks_completed() -> None:
    orch = _orchestrator(duration=0.0)
    asyncio.run(orch.process_issue(_issue(["dependency"])))
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

    asyncio.run(orch.process_issue(_issue(["security"])))
    asyncio.run(orch.refresh_active())

    task = store.list_tasks()[0]
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert orch.metrics().failed_sessions == 1


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        simulation_mode=True,
        database_path=":memory:",
        issue_polling_enabled=False,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_simulate_endpoint(client: TestClient) -> None:
    resp = client.post("/simulate/issue")
    assert resp.status_code == 200
    assert resp.json()["result"] == "triggered"


def test_poll_run_endpoint(client: TestClient) -> None:
    # Manual trigger scans the (simulated) repo and starts sessions.
    resp = client.post("/poll/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["triggered"] >= 5
    assert len(body["triggered_tasks"]) == body["triggered"]

    # Re-running the manual trigger dedupes against tracked issues.
    again = client.post("/poll/run").json()
    assert again["triggered"] == 0
    assert again["duplicate"] >= 5


def test_health_reports_monitored_repo(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "monitored_repo" in body
