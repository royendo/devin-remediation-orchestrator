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
"""End-to-end demo driver.

Fires a handful of synthetic remediation issues at a running orchestrator (in
simulation mode) and polls ``/metrics`` until every session has settled.

Usage:
    python scripts/demo.py [--base-url URL] [--count N]
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Orchestrator demo driver")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=10.0) as client:
        try:
            health = client.get("/health").json()
        except httpx.HTTPError as exc:
            print(f"Could not reach orchestrator at {base}: {exc}")
            print("Start it first, e.g. `docker compose up` or `make run-sim`.")
            return 1
        if not health.get("simulation_mode"):
            print(
                "Warning: orchestrator is in LIVE mode; this demo expects "
                "simulation mode."
            )

        print(f"Submitting {args.count} synthetic issue(s)...")
        for _ in range(args.count):
            resp = client.post("/simulate/issue").json()
            task = resp.get("task") or {}
            print(
                f"  [{resp['result']}] {task.get('title', '')} "
                f"-> session {task.get('session_id')}"
            )

        print("\nPolling /metrics until all sessions settle...")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            m = client.get("/metrics").json()
            print(
                f"  active={m['active_sessions']} completed={m['completed_sessions']} "
                f"failed={m['failed_sessions']} PRs={m['prs_created']}"
            )
            if m["active_sessions"] == 0 and m["pending"] == 0 and m["total"] > 0:
                break
            time.sleep(3)

        m = client.get("/metrics").json()
        print("\nFinal metrics:")
        for key, value in m.items():
            print(f"  {key}: {value}")
        print(f"\nDashboard: {base}/dashboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
