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
"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings, populated from the environment or a .env file."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Devin API ---
    devin_api_key: str = ""
    devin_org_id: str = ""
    devin_api_base: str = "https://api.devin.ai/v3"

    # --- GitHub ---
    github_webhook_secret: str = ""
    # Repository the standalone monitor polls for eligible issues.
    github_repo: str = "royendo/superset-devin"
    # Personal access token used to read issues (and lift the rate limit).
    # Optional for public repositories.
    github_token: str = ""
    github_api_base: str = "https://api.github.com"

    # --- Storage ---
    database_path: str = "data/orchestrator.db"

    # --- Behaviour ---
    simulation_mode: bool = False
    # How often the session-reconciliation worker refreshes active sessions.
    poll_interval_seconds: float = 15.0
    # Whether the standalone monitor scans the repository for issues. Disable to
    # run in pure webhook mode (or to keep tests deterministic).
    issue_polling_enabled: bool = True
    # How often the monitor scans the repository for new eligible issues.
    issue_poll_interval_seconds: float = 30.0
    trigger_labels: str = "devin-remediate,security,dependency,code-quality"

    # --- Simulation tuning (ignored unless simulation_mode is on) ---
    sim_session_duration_seconds: float = 20.0
    sim_failure_rate: float = 0.2

    # --- Server ---
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000

    @property
    def trigger_label_set(self) -> set[str]:
        """Return the configured trigger labels as a normalised set."""
        return {
            label.strip().lower()
            for label in self.trigger_labels.split(",")
            if label.strip()
        }

    @property
    def devin_configured(self) -> bool:
        """Whether real Devin credentials are available."""
        return bool(self.devin_api_key and self.devin_org_id)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
