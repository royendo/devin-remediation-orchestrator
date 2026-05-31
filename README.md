<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Devin Security Remediation Orchestrator

An event-driven automation that turns labeled GitHub issues into Devin
remediation sessions, tracks their progress, and reports observable outcomes.

> **Devin does the remediation. This service only orchestrates, tracks and
> reports.** It never edits code itself — it classifies eligible issues, starts
> Devin sessions through the Devin API, polls their status, and surfaces metrics
> and a dashboard.

## How it works

```
GitHub issue (opened, labeled)
        │  POST /webhooks/github
        ▼
┌─────────────────────────────┐
│  FastAPI orchestrator        │
│  1. verify signature         │
│  2. filter by label          │
│  3. dedupe by repo+issue     │
│  4. create Devin session ────┼──►  POST /v3/organizations/{org}/sessions
│  5. persist task (SQLite)    │
└─────────────────────────────┘
        ▲
        │  background polling worker
        │  GET /v3/organizations/{org}/sessions/{id}
        ▼
   status + PR URL  ──►  /dashboard (HTML)  +  /metrics (JSON)
```

A task is started only for **newly opened** issues carrying one of the trigger
labels (default `devin-remediate`, `security`, `dependency`, `code-quality`).

![Dashboard](docs/dashboard.png)

## Companion repository

This orchestrator was built to drive remediations on a fork of Apache Superset:
[`royendo/superset-devin`](https://github.com/royendo/superset-devin). That repo
contains the selected issues and their remediation PRs, plus a serverless
GitHub Actions trigger (`@Devin` comment / `devin-remediate` label) that starts a
Devin session directly from GitHub — the lightweight Phase 1 counterpart to this
service.

## Endpoints

| Method | Path                | Description                                       |
| ------ | ------------------- | ------------------------------------------------- |
| POST   | `/webhooks/github`  | GitHub `issues` webhook receiver (HMAC verified)  |
| POST   | `/simulate/issue`   | Inject a synthetic issue (simulation mode only)   |
| GET    | `/metrics`          | JSON metrics                                      |
| GET    | `/dashboard`        | HTML dashboard (auto-refreshing)                  |
| GET    | `/health`           | Liveness + current mode                           |

## Task state (SQLite)

Each task row stores: issue URL, issue number, repo, Devin session ID, Devin
session URL, status (`pending`/`running`/`completed`/`failed`), `created_at`,
`updated_at`, `completed_at`, PR URL (when available), and an error message on
failure.

## Environment variables

| Variable                       | Default                          | Notes                                            |
| ------------------------------ | -------------------------------- | ------------------------------------------------ |
| `SIMULATION_MODE`              | `false`                          | Use the built-in fake Devin client               |
| `DEVIN_API_KEY`                | —                                | Service-user key (`cog_...`); required for LIVE   |
| `DEVIN_ORG_ID`                 | —                                | Organization ID; required for LIVE               |
| `DEVIN_API_BASE`               | `https://api.devin.ai/v3`        | Devin API base URL                               |
| `GITHUB_WEBHOOK_SECRET`        | —                                | If empty, signature checks are skipped (dev only) |
| `DATABASE_PATH`                | `data/orchestrator.db`           | SQLite file path                                 |
| `POLL_INTERVAL_SECONDS`        | `15`                             | Session reconciliation interval                  |
| `TRIGGER_LABELS`               | `devin-remediate,security,dependency,code-quality` | Comma-separated      |
| `SIM_SESSION_DURATION_SECONDS` | `20`                             | Simulated session runtime                        |
| `SIM_FAILURE_RATE`             | `0.2`                            | Fraction of simulated sessions that fail         |
| `HOST` / `PORT`                | `0.0.0.0` / `8000`               | Bind address                                     |

If `DEVIN_API_KEY`/`DEVIN_ORG_ID` are missing, the service automatically falls
back to simulation mode.

## Quick start (Docker, simulation)

```bash
docker compose up --build
```

This boots in simulation mode (no credentials needed). Then, from another shell:

```bash
# fire 5 synthetic issues and watch them settle
docker compose exec orchestrator python scripts/demo.py
# or run the driver from the host:
python scripts/demo.py --base-url http://localhost:8000
```

Open the dashboard at <http://localhost:8000/dashboard> and metrics at
<http://localhost:8000/metrics>.

## Quick start (local Python)

```bash
make install          # creates .venv and installs deps + pytest
make run-sim          # starts uvicorn in simulation mode on :8000
# in another terminal:
make demo             # drives the end-to-end simulation
make test             # runs the test suite
```

## Local simulation explained

Simulation mode swaps the real Devin HTTP client for `SimulatedDevinClient`,
which reports each session as `running` for `SIM_SESSION_DURATION_SECONDS` and
then deterministically transitions to `finished` (with a fake PR URL) or `error`
based on a hash of the session id. This exercises the **entire** pipeline —
webhook parsing, label filtering, dedupe, persistence, the polling worker, the
dashboard and metrics — without any GitHub or Devin credentials.

`POST /simulate/issue` generates a realistic eligible issue and routes it through
the same `process_issue` path the webhook uses.

## Going LIVE

1. Create a Devin service user and key; note your org ID
   (Settings → Service Users). See the
   [Devin API docs](https://docs.devin.ai/api-reference/common-flows).
2. Set `SIMULATION_MODE=false`, `DEVIN_API_KEY`, `DEVIN_ORG_ID`, and
   `GITHUB_WEBHOOK_SECRET` (copy `.env.example` to `.env`).
3. Deploy the service somewhere GitHub can reach.

## GitHub webhook setup

In the target repository: **Settings → Webhooks → Add webhook**.

- **Payload URL:** `https://<your-host>/webhooks/github`
- **Content type:** `application/json`
- **Secret:** the same value as `GITHUB_WEBHOOK_SECRET`
- **Events:** *Let me select individual events* → check **Issues** only

For local testing you can forward webhooks with a tunnel (e.g. `cloudflared`,
`ngrok`) or replay a saved payload:

```bash
SECRET="your_secret"
BODY='{"action":"opened","issue":{"number":42,"title":"Bump urllib3",
  "body":"CVE fix","html_url":"https://github.com/o/r/issues/42",
  "labels":[{"name":"security"}]},"repository":{"full_name":"o/r"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')"
curl -X POST http://localhost:8000/webhooks/github \
  -H "X-GitHub-Event: issues" \
  -H "X-Hub-Signature-256: $SIG" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

## Project layout

```
app/
  config.py        # env-driven settings
  models.py        # pydantic models + status constants
  db.py            # SQLite task store
  devin_client.py  # real + simulated Devin clients
  github.py        # signature verify, payload parse, prompt build
  orchestrator.py  # core: classify -> start session -> reconcile -> metrics
  poller.py        # background reconciliation worker
  dashboard.py     # HTML rendering
  main.py          # FastAPI wiring
scripts/demo.py    # end-to-end demo driver
tests/             # unit + integration tests
```

## Tests

```bash
make test    # or: .venv/bin/python -m pytest tests/ -q
```
