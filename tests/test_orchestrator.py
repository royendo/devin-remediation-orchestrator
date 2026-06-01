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

import httpx
import pytest
from fastapi.testclient import TestClient

from app import github
from app.config import Settings
from app.db import TaskStore
from app.devin_client import SessionResult, SimulatedDevinClient
from app.issues import GitHubIssueSource, IssueSourceProtocol, SimulatedIssueSource
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

    assert first.ignored == 1  # the "question" issue has no trigger label

    # A second scan over the same issues must not start new sessions.
    second = asyncio.run(orch.scan_open_issues(source))
    assert second.triggered == 0
    assert second.duplicate == 2
    assert len(orch.store.list_tasks()) == 2

    # Runtime telemetry: funnel counts UNIQUE issues (not re-counted per scan),
    # while duplicate is a cumulative re-scan-skip signal.
    stats = orch.stats
    assert stats.scans_completed == 2
    assert stats.issues_detected_total == 3  # 3 distinct issues across 2 scans
    assert stats.eligible_total == 2
    assert stats.triggered_total == 2
    assert stats.ignored_total == 1  # the single "question" issue, counted once
    assert stats.duplicate_total == 2  # 2 eligible issues re-skipped on scan 2
    assert stats.last_scan_at is not None


def test_metrics_expose_observability_fields() -> None:
    orch = _orchestrator(duration=0.0)
    source = _StaticIssueSource(
        [_issue(["security"], number=1), _issue(["question"], number=2)]
    )
    asyncio.run(orch.scan_open_issues(source))
    asyncio.run(orch.refresh_active())  # completes the triggered session

    m = orch.metrics()
    assert m.completed_sessions == 1
    assert m.median_completion_seconds is not None
    assert m.throughput_per_hour > 0
    assert m.scans_completed == 1
    assert m.issues_detected_total == 2
    assert m.triggered_total == 1
    assert m.ignored_total == 1
    assert m.last_scan_at is not None
    # Timer polling is on by default, so a countdown is reported.
    assert m.next_scan_in_seconds is not None


def test_metrics_group_failure_reasons() -> None:
    settings = Settings(simulation_mode=True, database_path=":memory:")
    store = TaskStore(":memory:")
    client = SimulatedDevinClient(duration=0.0, failure_rate=1.0)
    orch = Orchestrator(settings, store, client)

    asyncio.run(orch.process_issue(_issue(["security"], number=1)))
    asyncio.run(orch.process_issue(_issue(["security"], number=2)))
    asyncio.run(orch.refresh_active())

    m = orch.metrics()
    assert m.failed_sessions == 2
    assert sum(m.failure_reasons.values()) == 2


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


class _MergedPRClient:
    """Stub client whose session stays ``running`` but exposes a merged PR."""

    async def create_session(self, prompt: str) -> SessionResult:
        return SessionResult("sid", "https://app.devin.ai/sessions/sid", "running")

    async def get_session(self, session_id: str) -> SessionResult:
        return SessionResult(
            session_id,
            "https://app.devin.ai/sessions/sid",
            "running",
            pr_url="https://github.com/o/r/pull/9",
            pr_state="merged",
        )

    async def aclose(self) -> None:
        return None


def test_merged_pr_completes_running_session() -> None:
    settings = Settings(simulation_mode=True, database_path=":memory:")
    store = TaskStore(":memory:")
    orch = Orchestrator(settings, store, _MergedPRClient())

    asyncio.run(orch.process_issue(_issue(["security"])))
    asyncio.run(orch.refresh_active())

    task = store.list_tasks()[0]
    # Session is still "running" but its PR merged → task is COMPLETED.
    assert task.status == TaskStatus.COMPLETED
    assert task.pr_url == "https://github.com/o/r/pull/9"
    assert task.completed_at is not None

    m = orch.metrics()
    assert m.completed_sessions == 1
    assert m.prs_created == 1
    assert m.success_rate == 1.0


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


def test_metrics_endpoint_includes_observability(client: TestClient) -> None:
    client.post("/poll/run")
    body = client.get("/metrics").json()
    for key in (
        "scans_completed",
        "issues_detected_total",
        "triggered_total",
        "ignored_total",
        "throughput_per_hour",
        "median_completion_seconds",
        "failure_reasons",
        "monitored_repo",
    ):
        assert key in body
    assert body["scans_completed"] >= 1


def test_poll_run_requires_token_when_configured() -> None:
    settings = Settings(
        simulation_mode=True,
        database_path=":memory:",
        issue_polling_enabled=False,
        poll_api_token="s3cret",
    )
    with TestClient(create_app(settings)) as c:
        assert c.post("/poll/run").status_code == 401
        assert c.post("/poll/run", headers={"X-Auth-Token": "wrong"}).status_code == 401
        ok = c.post("/poll/run", headers={"X-Auth-Token": "s3cret"})
        assert ok.status_code == 200
        # /simulate/issue is protected by the same token.
        assert c.post("/simulate/issue").status_code == 401
        assert (
            c.post("/simulate/issue", headers={"X-Auth-Token": "s3cret"}).status_code
            == 200
        )


def test_github_source_follows_pagination() -> None:
    page2 = (
        "https://api.github.com/repos/o/r/issues?state=open&per_page=100&page=2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json=[{"number": 3, "title": "c", "html_url": "u3", "labels": []}],
            )
        body = [
            {
                "number": 1,
                "title": "a",
                "html_url": "u1",
                "labels": [{"name": "security"}],
            },
            # A pull request masquerading as an issue — must be skipped.
            {
                "number": 2,
                "title": "pr",
                "html_url": "u2",
                "labels": [],
                "pull_request": {"url": "x"},
            },
        ]
        return httpx.Response(
            200, json=body, headers={"Link": f'<{page2}>; rel="next"'}
        )

    mock = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    source = GitHubIssueSource(Settings(github_repo="o/r"), client=mock)
    issues = asyncio.run(source.list_open_issues())
    asyncio.run(source.aclose())

    # PR (#2) skipped; page 2 (#3) followed via Link: rel="next".
    assert [i.number for i in issues] == [1, 3]

