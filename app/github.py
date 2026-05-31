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
"""Helpers for GitHub webhook verification, parsing and prompt building."""

from __future__ import annotations

import hashlib
import hmac

from app.models import GitHubIssue


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Validate a GitHub ``X-Hub-Signature-256`` header.

    Returns ``True`` when no secret is configured (local/dev mode) so the
    service stays usable without webhook secrets, and performs a constant-time
    comparison otherwise.
    """
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


def parse_issue_payload(payload: dict[str, object]) -> tuple[str, GitHubIssue | None]:
    """Extract the action and issue from a GitHub ``issues`` event payload.

    Returns ``(action, issue)`` where ``issue`` is ``None`` if the payload does
    not describe an issue (e.g. it is a ping or a different event type).
    """
    action = str(payload.get("action", ""))
    issue_obj = payload.get("issue")
    repo_obj = payload.get("repository")
    if not isinstance(issue_obj, dict) or not isinstance(repo_obj, dict):
        return action, None

    raw_labels = issue_obj.get("labels", [])
    labels: list[str] = []
    if isinstance(raw_labels, list):
        for entry in raw_labels:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                labels.append(entry["name"])
            elif isinstance(entry, str):
                labels.append(entry)

    issue = GitHubIssue(
        number=int(issue_obj.get("number", 0)),
        title=str(issue_obj.get("title", "")),
        body=str(issue_obj.get("body") or ""),
        html_url=str(issue_obj.get("html_url", "")),
        repo_full_name=str(repo_obj.get("full_name", "")),
        labels=labels,
    )
    return action, issue


def has_trigger_label(issue: GitHubIssue, trigger_labels: set[str]) -> bool:
    """Whether an issue carries at least one configured trigger label."""
    issue_labels = {label.lower() for label in issue.labels}
    return bool(issue_labels & trigger_labels)


def is_eligible(action: str, issue: GitHubIssue, trigger_labels: set[str]) -> bool:
    """Whether a newly opened issue should trigger a remediation session."""
    if action != "opened":
        return False
    return has_trigger_label(issue, trigger_labels)


def build_prompt(issue: GitHubIssue) -> str:
    """Build the instruction Devin receives for a remediation issue."""
    labels = ", ".join(issue.labels) or "none"
    return (
        "You are an automated maintenance worker for the repository "
        f"{issue.repo_full_name}. Remediate the following GitHub issue and open "
        "a pull request with the fix.\n\n"
        f"Issue #{issue.number}: {issue.title}\n"
        f"URL: {issue.html_url}\n"
        f"Labels: {labels}\n\n"
        "Description:\n"
        f"{issue.body or '(no description provided)'}\n\n"
        "Requirements:\n"
        "- Make the minimal, focused change needed to resolve the issue.\n"
        "- Follow the repository's existing conventions and run its linters/tests.\n"
        "- Open a pull request and reference this issue in the description.\n"
        "- If the issue cannot be safely remediated, explain why instead of guessing."
    )
