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
"""Pydantic models and status constants used across the service."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Lifecycle of a remediation task tracked by the orchestrator."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# Devin session statuses that mean the session finished successfully.
DEVIN_TERMINAL_OK = {"finished", "completed", "exit", "exited", "blocked"}
# Devin session statuses that mean the session ended in failure.
DEVIN_TERMINAL_FAIL = {"error", "failed", "expired", "terminated", "cancelled"}


class GitHubIssue(BaseModel):
    """The subset of a GitHub issue the orchestrator cares about."""

    number: int
    title: str
    body: str = ""
    html_url: str
    repo_full_name: str
    labels: list[str]


class Task(BaseModel):
    """A remediation task persisted in SQLite."""

    id: int
    repo: str
    issue_number: int
    issue_url: str
    title: str
    labels: list[str]
    status: TaskStatus
    devin_status: str | None = None
    session_id: str | None = None
    session_url: str | None = None
    pr_url: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class Metrics(BaseModel):
    """Aggregated metrics exposed via ``/metrics``.

    Designed to answer, at a glance, "is this system working?":

    * **Task state** — ``pending``/``active_sessions``/``completed_sessions``/
      ``failed_sessions`` show where every tracked issue is in its lifecycle.
    * **Success/failure signals** — ``success_rate`` plus ``failure_reasons``
      (grouped error messages) say *whether* remediations land and *why* they
      don't.
    * **Throughput / lead time** — ``throughput_per_hour`` and the
      average/median completion times show how fast work flows through.
    * **Liveness** — ``last_scan_at``/``scans_completed``/``uptime_seconds``/
      ``next_scan_in_seconds`` prove the monitor is actually polling.
    """

    # --- Task state (derived from the persisted store) ---
    total: int
    pending: int
    active_sessions: int
    completed_sessions: int
    failed_sessions: int
    prs_created: int

    # --- Outcome signals ---
    success_rate: float
    failure_reasons: dict[str, int] = Field(default_factory=dict)

    # --- Throughput / lead time ---
    average_completion_seconds: float | None = None
    median_completion_seconds: float | None = None
    throughput_per_hour: float = 0.0

    # --- Liveness / scan funnel (runtime counters since process start) ---
    monitored_repo: str = ""
    live_mode: bool = False
    scans_completed: int = 0
    issues_detected_total: int = 0
    eligible_total: int = 0
    triggered_total: int = 0
    ignored_total: int = 0
    duplicate_total: int = 0
    last_scan_at: str | None = None
    next_scan_in_seconds: float | None = None
    uptime_seconds: float = 0.0
