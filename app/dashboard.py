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
"""Render a minimal, dependency-free HTML dashboard."""

from __future__ import annotations

from html import escape

from app.models import Metrics, Task, TaskStatus

_STATUS_COLOURS = {
    TaskStatus.PENDING: "#9aa0a6",
    TaskStatus.RUNNING: "#1a73e8",
    TaskStatus.COMPLETED: "#188038",
    TaskStatus.FAILED: "#d93025",
}


def _link(url: str | None, text: str | None = None) -> str:
    if not url:
        return "&mdash;"
    label = escape(text or url)
    return f'<a href="{escape(url)}" target="_blank" rel="noopener">{label}</a>'


def _metric_card(label: str, value: str) -> str:
    return (
        '<div class="card">'
        f'<div class="value">{escape(value)}</div>'
        f'<div class="label">{escape(label)}</div>'
        "</div>"
    )


def _task_row(task: Task) -> str:
    colour = _STATUS_COLOURS.get(task.status, "#000")
    badge = (
        f'<span class="badge" style="background:{colour}">'
        f"{escape(task.status.value)}</span>"
    )
    return (
        "<tr>"
        f"<td>{task.id}</td>"
        f"<td>{escape(task.repo)}</td>"
        f"<td>{_link(task.issue_url, f'#{task.issue_number}')}</td>"
        f'<td class="title">{escape(task.title)}</td>'
        f"<td>{escape(', '.join(task.labels))}</td>"
        f"<td>{badge}</td>"
        f"<td>{_link(task.session_url, task.session_id)}</td>"
        f"<td>{_link(task.pr_url, 'PR')}</td>"
        "</tr>"
    )


def render_dashboard(metrics: Metrics, tasks: list[Task]) -> str:
    """Return a full HTML page summarising orchestrator activity."""
    avg = (
        f"{metrics.average_completion_seconds:.1f}s"
        if metrics.average_completion_seconds is not None
        else "—"
    )
    cards = "".join(
        [
            _metric_card("Active", str(metrics.active_sessions)),
            _metric_card("Completed", str(metrics.completed_sessions)),
            _metric_card("Failed", str(metrics.failed_sessions)),
            _metric_card("PRs created", str(metrics.prs_created)),
            _metric_card("Avg. completion", avg),
            _metric_card("Success rate", f"{metrics.success_rate * 100:.0f}%"),
        ]
    )
    rows = (
        "".join(_task_row(t) for t in tasks)
        if tasks
        else '<tr><td colspan="8" class="empty">No tasks yet</td></tr>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>Devin Remediation Orchestrator</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; background: #f5f6f8; color: #202124; }}
  header {{ background: #202124; color: #fff; padding: 18px 28px; }}
  header h1 {{ margin: 0; font-size: 18px; font-weight: 600; }}
  header p {{ margin: 4px 0 0; color: #9aa0a6; font-size: 13px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 14px; padding: 22px 28px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 16px 20px;
          min-width: 130px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card .value {{ font-size: 26px; font-weight: 700; }}
  .card .label {{ font-size: 12px; color: #5f6368; margin-top: 4px; }}
  table {{ width: calc(100% - 56px); margin: 0 28px 32px; border-collapse: collapse;
          background: #fff; border-radius: 10px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  th, td {{ text-align: left; padding: 10px 14px; font-size: 13px;
           border-bottom: 1px solid #eee; }}
  th {{ background: #fafafa; color: #5f6368; text-transform: uppercase;
       font-size: 11px; letter-spacing: .04em; }}
  td.title {{ max-width: 320px; }}
  td.empty {{ text-align: center; color: #9aa0a6; padding: 28px; }}
  .badge {{ color: #fff; padding: 3px 9px; border-radius: 999px; font-size: 11px; }}
  a {{ color: #1a73e8; text-decoration: none; }}
</style>
</head>
<body>
<header>
  <h1>Devin Security Remediation Orchestrator</h1>
  <p>Devin does the remediation &middot; this service orchestrates, tracks and reports
     &middot; auto-refreshes every 5s</p>
</header>
<section class="cards">{cards}</section>
<table>
  <thead><tr>
    <th>ID</th><th>Repo</th><th>Issue</th><th>Title</th><th>Labels</th>
    <th>Status</th><th>Session</th><th>PR</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</body>
</html>"""
