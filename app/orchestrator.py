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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median

from app import github
from app.config import Settings
from app.db import TaskStore, utcnow
from app.devin_client import DevinClientProtocol
from app.issues import IssueSourceProtocol
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


@dataclass
class ScanResult:
    """Summary of a single repository scan for eligible issues."""

    scanned: int = 0
    eligible: int = 0
    triggered: int = 0
    duplicate: int = 0
    ignored: int = 0
    errors: int = 0
    triggered_tasks: list[Task] = field(default_factory=list)


@dataclass
class RuntimeStats:
    """In-memory scan telemetry accumulated since the process started.

    These counters reset on restart (unlike the persisted task metrics); they
    exist to answer "is the monitor actually polling, and what is it seeing?".
    """

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scans_completed: int = 0
    issues_detected_total: int = 0
    eligible_total: int = 0
    triggered_total: int = 0
    ignored_total: int = 0
    duplicate_total: int = 0
    error_total: int = 0
    last_scan_at: str | None = None

    def record(self, result: ScanResult) -> None:
        self.scans_completed += 1
        self.issues_detected_total += result.scanned
        self.eligible_total += result.eligible
        self.triggered_total += result.triggered
        self.ignored_total += result.ignored
        self.duplicate_total += result.duplicate
        self.error_total += result.errors
        self.last_scan_at = utcnow()


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
        self._stats = RuntimeStats()

    @property
    def store(self) -> TaskStore:
        return self._store

    @property
    def stats(self) -> RuntimeStats:
        return self._stats

    async def process_issue(self, issue: GitHubIssue) -> IssueOutcome:
        """Classify a single issue and, if eligible, start a session."""
        if not github.has_trigger_label(issue, self._settings.trigger_label_set):
            return IssueOutcome(
                "ignored",
                f"labels={issue.labels} did not match triggers",
            )
        return await self._trigger(issue)

    async def scan_open_issues(self, source: IssueSourceProtocol) -> ScanResult:
        """Pull open issues from ``source`` and start sessions for new ones.

        This is the heart of the standalone monitor: it is safe to call on a
        timer or on demand, and dedupes against already-tracked issues so the
        same issue never spawns two sessions.
        """
        issues = await source.list_open_issues()
        result = ScanResult(scanned=len(issues))
        triggers = self._settings.trigger_label_set
        for issue in issues:
            if not github.has_trigger_label(issue, triggers):
                result.ignored += 1
                continue
            result.eligible += 1
            outcome = await self._trigger(issue)
            if outcome.result == "triggered":
                result.triggered += 1
                if outcome.task is not None:
                    result.triggered_tasks.append(outcome.task)
            elif outcome.result == "duplicate":
                result.duplicate += 1
            elif outcome.result == "error":
                result.errors += 1
        self._stats.record(result)
        # Always emit a structured, log-greppable summary of the scan funnel so
        # the system is observable even without the dashboard or /metrics.
        logger.info(
            "scan complete scanned=%s eligible=%s triggered=%s "
            "duplicate=%s ignored=%s errors=%s",
            result.scanned,
            result.eligible,
            result.triggered,
            result.duplicate,
            result.ignored,
            result.errors,
        )
        return result

    async def _trigger(self, issue: GitHubIssue) -> IssueOutcome:
        """Dedupe, persist and start a Devin session for ``issue``."""
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
        med = median(durations) if durations else None

        finished = completed + failed
        success_rate = (completed / finished) if finished else 0.0

        failure_reasons = Counter(
            _short_reason(t.error)
            for t in tasks
            if t.status == TaskStatus.FAILED
        )

        stats = self._stats
        now = datetime.now(timezone.utc)
        uptime = max((now - stats.started_at).total_seconds(), 0.0)
        throughput = (completed / uptime * 3600.0) if uptime > 0 else 0.0

        return Metrics(
            total=len(tasks),
            pending=pending,
            active_sessions=active,
            completed_sessions=completed,
            failed_sessions=failed,
            prs_created=prs,
            success_rate=round(success_rate, 4),
            failure_reasons=dict(failure_reasons),
            average_completion_seconds=(round(avg, 2) if avg is not None else None),
            median_completion_seconds=(round(med, 2) if med is not None else None),
            throughput_per_hour=round(throughput, 2),
            monitored_repo=self._settings.github_repo,
            live_mode=not (
                self._settings.simulation_mode
                or not self._settings.devin_configured
            ),
            scans_completed=stats.scans_completed,
            issues_detected_total=stats.issues_detected_total,
            eligible_total=stats.eligible_total,
            triggered_total=stats.triggered_total,
            ignored_total=stats.ignored_total,
            duplicate_total=stats.duplicate_total,
            last_scan_at=stats.last_scan_at,
            next_scan_in_seconds=self._next_scan_in_seconds(now),
            uptime_seconds=round(uptime, 1),
        )

    def _next_scan_in_seconds(self, now: datetime) -> float | None:
        """Seconds until the next scheduled scan, if timer polling is on."""
        if not self._settings.issue_polling_enabled:
            return None
        if self._stats.last_scan_at is None:
            return 0.0
        last = datetime.fromisoformat(self._stats.last_scan_at)
        elapsed = (now - last).total_seconds()
        remaining = self._settings.issue_poll_interval_seconds - elapsed
        return round(max(remaining, 0.0), 1)


def _short_reason(error: str | None) -> str:
    """Collapse a (possibly long) error into a stable, groupable label."""
    if not error:
        return "unknown"
    first_line = error.strip().splitlines()[0]
    return first_line[:80]


def _duration_seconds(task: Task) -> float:
    start = datetime.fromisoformat(task.created_at)
    end = datetime.fromisoformat(task.completed_at) if task.completed_at else start
    return max((end - start).total_seconds(), 0.0)
