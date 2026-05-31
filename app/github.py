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
"""Helpers for GitHub issue eligibility and Devin prompt building."""

from __future__ import annotations

from app.models import GitHubIssue


def has_trigger_label(issue: GitHubIssue, trigger_labels: set[str]) -> bool:
    """Whether an issue carries at least one configured trigger label."""
    issue_labels = {label.lower() for label in issue.labels}
    return bool(issue_labels & trigger_labels)


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
