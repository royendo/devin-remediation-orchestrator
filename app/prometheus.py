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
"""Render metrics in the Prometheus text exposition format.

Dependency-free so the service can be scraped by Prometheus/Grafana/Datadog
without pulling in the official client library. This is the standard path an
engineering org uses to build leadership dashboards and alerts.
"""

from __future__ import annotations

from app.models import Metrics

_PREFIX = "orchestrator"


def _line(name: str, value: float, help_text: str, kind: str) -> list[str]:
    metric = f"{_PREFIX}_{name}"
    return [
        f"# HELP {metric} {help_text}",
        f"# TYPE {metric} {kind}",
        f"{metric} {value}",
    ]


def render_prometheus(metrics: Metrics) -> str:
    """Return ``metrics`` formatted for the Prometheus text exposition."""
    lines: list[str] = []

    # Gauges — current task state.
    lines += _line("tasks_total", metrics.total, "Tasks ever tracked", "gauge")
    lines += _line("tasks_pending", metrics.pending, "Tasks pending", "gauge")
    lines += _line(
        "tasks_active", metrics.active_sessions, "Active Devin sessions", "gauge"
    )
    lines += _line(
        "tasks_completed", metrics.completed_sessions, "Completed tasks", "gauge"
    )
    lines += _line("tasks_failed", metrics.failed_sessions, "Failed tasks", "gauge")
    lines += _line("prs_created", metrics.prs_created, "Pull requests opened", "gauge")

    # Outcome / efficiency.
    lines += _line(
        "success_rate", metrics.success_rate, "Completed / finished tasks", "gauge"
    )
    lines += _line(
        "throughput_per_hour",
        metrics.throughput_per_hour,
        "Completed tasks per hour since start",
        "gauge",
    )
    if metrics.average_completion_seconds is not None:
        lines += _line(
            "avg_completion_seconds",
            metrics.average_completion_seconds,
            "Average issue-to-completion seconds",
            "gauge",
        )
    if metrics.median_completion_seconds is not None:
        lines += _line(
            "median_completion_seconds",
            metrics.median_completion_seconds,
            "Median issue-to-completion seconds",
            "gauge",
        )

    # Liveness / scan funnel — counters since process start.
    lines += _line(
        "scans_completed_total",
        metrics.scans_completed,
        "Repository scans run since start",
        "counter",
    )
    lines += _line(
        "issues_detected_total",
        metrics.issues_detected_total,
        "Issues seen across all scans",
        "counter",
    )
    lines += _line(
        "issues_triggered_total",
        metrics.triggered_total,
        "Sessions started across all scans",
        "counter",
    )
    lines += _line(
        "issues_ignored_total",
        metrics.ignored_total,
        "Issues skipped (no trigger label)",
        "counter",
    )
    lines += _line(
        "uptime_seconds", metrics.uptime_seconds, "Process uptime", "gauge"
    )

    # Per-reason failure breakdown as a labelled gauge.
    if metrics.failure_reasons:
        metric = f"{_PREFIX}_failures_by_reason"
        lines.append(f"# HELP {metric} Failed tasks grouped by error reason")
        lines.append(f"# TYPE {metric} gauge")
        for reason, count in metrics.failure_reasons.items():
            safe = reason.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{metric}{{reason="{safe}"}} {count}')

    return "\n".join(lines) + "\n"
