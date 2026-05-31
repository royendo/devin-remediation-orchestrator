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

.PHONY: install run-sim run test demo docker-up docker-down

install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest

run-sim:
	SIMULATION_MODE=true POLL_INTERVAL_SECONDS=5 SIM_SESSION_DURATION_SECONDS=15 \
		.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

run:
	.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

test:
	.venv/bin/python -m pytest tests/ -q

demo:
	.venv/bin/python scripts/demo.py

docker-up:
	docker compose up --build

docker-down:
	docker compose down -v
