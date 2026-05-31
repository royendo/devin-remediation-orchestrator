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
"""Synthetic issue generation for local simulation and demos."""

from __future__ import annotations

import itertools

from app.models import GitHubIssue

_REPO = "royendo/superset-devin"

_TEMPLATES: list[tuple[str, str, list[str]]] = [
    (
        "Bump urllib3 to patch CVE-2024-37891 (proxy auth leak)",
        "Dependency scanner flagged urllib3 < 2.2.2 which leaks proxy-auth "
        "headers on cross-origin redirects. Upgrade and verify the lockfile.",
        ["dependency", "security", "devin-remediate"],
    ),
    (
        "Sanitise user input rendered in error template (XSS)",
        "Static analysis reports unescaped user input reaching an HTML "
        "response. Escape the value and add a regression test.",
        ["security", "devin-remediate"],
    ),
    (
        "Replace deprecated datetime.utcnow() usages",
        "datetime.utcnow() is deprecated in Python 3.12. Replace with "
        "timezone-aware datetime.now(timezone.utc).",
        ["code-quality", "devin-remediate"],
    ),
    (
        "Pin GitHub Actions to commit SHAs",
        "Workflow actions are referenced by mutable tags. Pin them to commit "
        "SHAs to harden the supply chain.",
        ["security", "dependency"],
    ),
    (
        "Remove unused imports flagged by ruff",
        "ruff reports several F401 unused imports across the codebase. "
        "Remove them to keep the lint baseline clean.",
        ["code-quality"],
    ),
]

_counter = itertools.count(1)


def make_issue() -> GitHubIssue:
    """Return the next synthetic eligible issue."""
    number = next(_counter)
    title, body, labels = _TEMPLATES[(number - 1) % len(_TEMPLATES)]
    issue_number = 1000 + number
    return GitHubIssue(
        number=issue_number,
        title=title,
        body=body,
        html_url=f"https://github.com/{_REPO}/issues/{issue_number}",
        repo_full_name=_REPO,
        labels=labels,
    )
