#!/usr/bin/env python3
"""Quick smoke test for real E2E run with gpt-oss:120b."""
import json
import os
import sys
import time
from pathlib import Path

# Ensure src is on path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

os.environ["STA_DB_PATH"] = "./sta_audit.sqlite3"

from starlette.testclient import TestClient
from sta.app.api import create_app
from sta.config import get_settings

get_settings.cache_clear()
settings = get_settings()
print("Settings:", settings.safe_summary())

app = create_app()
client = TestClient(app)

resp = client.post("/api/runs", json={"table_name": "local.demo.healthy_table"})
print("create:", resp.status_code, resp.json())
run_id = resp.json()["run_id"]

start = time.monotonic()
while True:
    run = client.get(f"/api/runs/{run_id}").json()
    if run["status"] in ("completed", "failed", "cancelled"):
        break
    print("status:", run["status"], run.get("phase"), f"elapsed {time.monotonic()-start:.1f}s")
    time.sleep(2)

elapsed = time.monotonic() - start
print("terminal:", json.dumps(run, indent=2))
print(f"elapsed {elapsed:.1f}s")

results = client.get(f"/api/runs/{run_id}/results").json()
print("results:", json.dumps(results, indent=2)[:2000])

report = client.get(f"/api/runs/{run_id}/report")
print("report status:", report.status_code)
if report.status_code == 200:
    print("report:", json.dumps(report.json(), indent=2)[:3000])
else:
    print("report body:", report.text[:500])

# events
events = client.get(f"/api/runs/{run_id}/events").text
print("events:", events[:2000])
