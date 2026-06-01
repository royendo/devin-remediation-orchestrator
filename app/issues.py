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
"""Issue sources for the standalone monitor.

The monitor polls a *source* for the current set of open issues.
``GitHubIssueSource`` reads them from the GitHub REST API;
``SimulatedIssueSource`` returns a deterministic set so the whole pipeline can
be exercised offline with no credentials.
"""

from __future__ import annotations

import re
from typing import Protocol

import httpx

from app import simulation
from app.config import Settings
from app.models import GitHubIssue

# Caps pagination so a misbehaving API can never spin the monitor forever
# (GitHub returns at most 100 issues/page, so this covers 10k issues).
_MAX_PAGES = 100
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def _next_link(link_header: str | None) -> str | None:
    """Return the ``rel="next"`` URL from a GitHub ``Link`` header, if any."""
    if not link_header:
        return None
    match = _LINK_NEXT.search(link_header)
    return match.group(1) if match else None


class IssueSourceProtocol(Protocol):
    """Interface implemented by both the real and simulated issue sources."""

    async def list_open_issues(self) -> list[GitHubIssue]: ...

    async def aclose(self) -> None: ...


def _parse_labels(raw_labels: object) -> list[str]:
    labels: list[str] = []
    if isinstance(raw_labels, list):
        for entry in raw_labels:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                labels.append(entry["name"])
            elif isinstance(entry, str):
                labels.append(entry)
    return labels


class GitHubIssueSource:
    """Reads open issues for a repository from the GitHub REST API."""

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self._repo = settings.github_repo
        self._base = settings.github_api_base.rstrip("/")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        # ``client`` is injectable so tests can supply a MockTransport.
        self._client = client or httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=httpx.Timeout(30.0),
        )

    async def list_open_issues(self) -> list[GitHubIssue]:
        """Return every open issue, following ``Link: rel="next"`` pagination.

        GitHub caps a page at 100 items, so a repo with more open issues than
        that would otherwise be silently truncated to the first page.
        """
        issues: list[GitHubIssue] = []
        # First request is relative (uses base_url + params); subsequent pages
        # use the absolute ``next`` URL from the Link header, which already
        # carries the query string, so params are dropped after page 1.
        url: str | None = f"/repos/{self._repo}/issues"
        params: dict[str, str | int] | None = {"state": "open", "per_page": 100}
        pages = 0
        while url is not None and pages < _MAX_PAGES:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            for item in resp.json():
                issue = self._to_issue(item)
                if issue is not None:
                    issues.append(issue)
            url = _next_link(resp.headers.get("link"))
            params = None
            pages += 1
        return issues

    def _to_issue(self, item: object) -> GitHubIssue | None:
        if not isinstance(item, dict):
            return None
        # The issues endpoint also returns pull requests; skip them.
        if item.get("pull_request") is not None:
            return None
        return GitHubIssue(
            number=int(item.get("number", 0)),
            title=str(item.get("title", "")),
            body=str(item.get("body") or ""),
            html_url=str(item.get("html_url", "")),
            repo_full_name=self._repo,
            labels=_parse_labels(item.get("labels", [])),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class SimulatedIssueSource:
    """Returns a fixed, deterministic set of eligible issues for demos."""

    def __init__(self, repo: str = "royendo/superset-devin") -> None:
        self._issues = simulation.sample_issues(repo=repo)

    async def list_open_issues(self) -> list[GitHubIssue]:
        return list(self._issues)

    async def aclose(self) -> None:
        return None


def build_issue_source(settings: Settings) -> IssueSourceProtocol:
    """Return the appropriate issue source for the current configuration."""
    if settings.simulation_mode or not settings.devin_configured:
        return SimulatedIssueSource(repo=settings.github_repo)
    return GitHubIssueSource(settings)
