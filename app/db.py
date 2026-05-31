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
"""SQLite-backed task store.

The store is intentionally tiny: a single ``tasks`` table plus a handful of
helpers.  ``sqlite3`` from the standard library keeps the dependency surface
minimal, which matches the "clean, minimal Python" brief.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.models import Task, TaskStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo          TEXT NOT NULL,
    issue_number  INTEGER NOT NULL,
    issue_url     TEXT NOT NULL,
    title         TEXT NOT NULL,
    labels        TEXT NOT NULL,
    status        TEXT NOT NULL,
    devin_status  TEXT,
    session_id    TEXT,
    session_url   TEXT,
    pr_url        TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT,
    UNIQUE (repo, issue_number)
);
"""


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Thread-safe wrapper around a SQLite database file."""

    def __init__(self, path: str) -> None:
        self._path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` lets the polling worker and the request
        # handlers share one connection; a lock serialises access.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            repo=row["repo"],
            issue_number=row["issue_number"],
            issue_url=row["issue_url"],
            title=row["title"],
            labels=json.loads(row["labels"]),
            status=TaskStatus(row["status"]),
            devin_status=row["devin_status"],
            session_id=row["session_id"],
            session_url=row["session_url"],
            pr_url=row["pr_url"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def get_by_issue(self, repo: str, issue_number: int) -> Task | None:
        """Return the task for ``repo``/``issue_number`` if it exists."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM tasks WHERE repo = ? AND issue_number = ?",
                (repo, issue_number),
            )
            row = cur.fetchone()
        return self._row_to_task(row) if row else None

    def create_task(
        self,
        *,
        repo: str,
        issue_number: int,
        issue_url: str,
        title: str,
        labels: list[str],
    ) -> Task:
        """Insert a new pending task and return it."""
        now = utcnow()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (
                    repo, issue_number, issue_url, title, labels,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo,
                    issue_number,
                    issue_url,
                    title,
                    json.dumps(labels),
                    TaskStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            task_id = cur.lastrowid
        created = self.get_by_id(int(task_id)) if task_id is not None else None
        assert created is not None  # noqa: S101
        return created

    def get_by_id(self, task_id: int) -> Task | None:
        """Return a task by its primary key."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
        return self._row_to_task(row) if row else None

    def update_task(
        self,
        task_id: int,
        *,
        status: TaskStatus | None = None,
        devin_status: str | None = None,
        session_id: str | None = None,
        session_url: str | None = None,
        pr_url: str | None = None,
        error: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        """Update the supplied (non-None) fields on a task."""
        fields: dict[str, object] = {"updated_at": utcnow()}
        if status is not None:
            fields["status"] = status.value
        if devin_status is not None:
            fields["devin_status"] = devin_status
        if session_id is not None:
            fields["session_id"] = session_id
        if session_url is not None:
            fields["session_url"] = session_url
        if pr_url is not None:
            fields["pr_url"] = pr_url
        if error is not None:
            fields["error"] = error
        if completed_at is not None:
            fields["completed_at"] = completed_at

        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values())
        values.append(task_id)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",  # noqa: S608
                values,
            )

    def list_tasks(self) -> list[Task]:
        """Return all tasks, newest first."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM tasks ORDER BY id DESC")
            rows = cur.fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_active(self) -> list[Task]:
        """Return tasks whose Devin session may still be running."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?) ORDER BY id ASC",
                (TaskStatus.PENDING.value, TaskStatus.RUNNING.value),
            )
            rows = cur.fetchall()
        return [self._row_to_task(row) for row in rows]
