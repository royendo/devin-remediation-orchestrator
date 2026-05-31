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

from pydantic import BaseModel


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
    """Aggregated metrics exposed via ``/metrics``."""

    total: int
    pending: int
    active_sessions: int
    completed_sessions: int
    failed_sessions: int
    prs_created: int
    success_rate: float
    average_completion_seconds: float | None
