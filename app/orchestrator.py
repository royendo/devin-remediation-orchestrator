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
"""Core orchestration logic.

The orchestrator is deliberately the *only* place that decides what to do with
an issue.  It never edits code itself: it classifies issues, hands eligible
ones to Devin, persists task state and reconciles session status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app import github
from app.config import Settings
from app.db import TaskStore, utcnow
from app.devin_client import DevinClientProtocol
from app.models import (
    DEVIN_TERMINAL_FAIL,
    DEVIN_TERMINAL_OK,
    GitHubIssue,
    Metrics,
    Task,
    TaskStatus,
)

logger = logging.getLogger("orchestrator")


@dataclass
class IssueOutcome:
    """Result of processing a single issue event."""

    result: str  # one of: triggered, ignored, duplicate, error
    detail: str
    task: Task | None = None


class Orchestrator:
    """Glue between GitHub issue events, the task store and Devin."""

    def __init__(
        self,
        settings: Settings,
        store: TaskStore,
        client: DevinClientProtocol,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client

    @property
    def store(self) -> TaskStore:
        return self._store

    async def process_issue(self, action: str, issue: GitHubIssue) -> IssueOutcome:
        """Classify an issue and, if eligible, start a Devin session."""
        if not github.is_eligible(action, issue, self._settings.trigger_label_set):
            return IssueOutcome(
                "ignored",
                f"action={action!r} labels={issue.labels} did not match triggers",
            )

        existing = self._store.get_by_issue(issue.repo_full_name, issue.number)
        if existing is not None:
            return IssueOutcome(
                "duplicate",
                f"task #{existing.id} already tracks this issue",
                existing,
            )

        task = self._store.create_task(
            repo=issue.repo_full_name,
            issue_number=issue.number,
            issue_url=issue.html_url,
            title=issue.title,
            labels=issue.labels,
        )

        prompt = github.build_prompt(issue)
        try:
            session = await self._client.create_session(prompt)
        except Exception as exc:  # noqa: BLE001 - surface any client failure
            logger.exception("Failed to create Devin session for issue")
            self._store.update_task(
                task.id,
                status=TaskStatus.FAILED,
                error=f"create_session failed: {exc}",
                completed_at=utcnow(),
            )
            refreshed = self._store.get_by_id(task.id)
            return IssueOutcome("error", str(exc), refreshed)

        self._store.update_task(
            task.id,
            status=TaskStatus.RUNNING,
            devin_status=session.status,
            session_id=session.session_id,
            session_url=session.session_url,
            pr_url=session.pr_url,
        )
        refreshed = self._store.get_by_id(task.id)
        logger.info(
            "Started session %s for %s#%s",
            session.session_id,
            issue.repo_full_name,
            issue.number,
        )
        return IssueOutcome("triggered", "session created", refreshed)

    async def refresh_active(self) -> int:
        """Poll active sessions and update their state. Returns count updated."""
        updated = 0
        for task in self._store.list_active():
            if task.session_id is None:
                continue
            try:
                session = await self._client.get_session(task.session_id)
            except Exception:  # noqa: BLE001 - a transient poll error is non-fatal
                logger.exception("Polling session %s failed", task.session_id)
                continue

            devin_status = session.status.lower()
            if devin_status in DEVIN_TERMINAL_FAIL:
                self._store.update_task(
                    task.id,
                    status=TaskStatus.FAILED,
                    devin_status=session.status,
                    pr_url=session.pr_url,
                    error=session.error or "Devin session ended in failure",
                    completed_at=utcnow(),
                )
                updated += 1
            elif devin_status in DEVIN_TERMINAL_OK:
                self._store.update_task(
                    task.id,
                    status=TaskStatus.COMPLETED,
                    devin_status=session.status,
                    pr_url=session.pr_url,
                    completed_at=utcnow(),
                )
                updated += 1
            else:
                self._store.update_task(
                    task.id,
                    devin_status=session.status,
                    pr_url=session.pr_url,
                )
        return updated

    def metrics(self) -> Metrics:
        """Compute aggregate metrics across all tracked tasks."""
        tasks = self._store.list_tasks()
        pending = sum(t.status == TaskStatus.PENDING for t in tasks)
        active = sum(t.status == TaskStatus.RUNNING for t in tasks)
        completed = sum(t.status == TaskStatus.COMPLETED for t in tasks)
        failed = sum(t.status == TaskStatus.FAILED for t in tasks)
        prs = sum(bool(t.pr_url) for t in tasks)

        durations = [
            _duration_seconds(t)
            for t in tasks
            if t.status == TaskStatus.COMPLETED and t.completed_at
        ]
        avg = sum(durations) / len(durations) if durations else None

        finished = completed + failed
        success_rate = (completed / finished) if finished else 0.0

        return Metrics(
            total=len(tasks),
            pending=pending,
            active_sessions=active,
            completed_sessions=completed,
            failed_sessions=failed,
            prs_created=prs,
            success_rate=round(success_rate, 4),
            average_completion_seconds=(round(avg, 2) if avg is not None else None),
        )


def _duration_seconds(task: Task) -> float:
    start = datetime.fromisoformat(task.created_at)
    end = datetime.fromisoformat(task.completed_at) if task.completed_at else start
    return max((end - start).total_seconds(), 0.0)
