#!/usr/bin/env python3
"""Independent E2E benchmark audit of STA with gpt-oss:120b and Docker-local tables.

Runs representative scenarios against the real default app (uvicorn) and
produces an evidence-backed verdict for each table:
  - terminal status / error
  - produced Rxxx evidence (tools, row counts, key payload facts)
  - final report claims
  - event stream summary
  - claim validation against known seeded table properties
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE_URL = "http://127.0.0.1:8002"
DB_PATH = Path("/tmp/sta_audit.sqlite3")
SCENARIOS = [
    ("healthy_table", "demo.healthy_table"),
    ("small_files_table", "demo.small_files_table"),
    ("delete_files_table", "demo.delete_files_table"),
]

# Known seeded properties from scripts/seed_local.py
SEED_FACTS = {
    "demo.healthy_table": {
        "rows": 10_000,
        "files": 1,
        "partitioned": False,
        "sort_order": "id asc",
        "delete_files": False,
    },
    "demo.small_files_table": {
        "rows": 100 * 10,
        "files": 100,
        "partitioned": False,
        "sort_order": None,
        "delete_files": False,
    },
    "demo.delete_files_table": {
        "rows": 2_000,
        "delete_predicate": "id < 500",
        "partitioned": False,
        "sort_order": None,
        "delete_files": True,
    },
}


def create_run(table_name: str) -> str:
    resp = requests.post(
        f"{BASE_URL}/api/runs",
        json={"table_name": table_name},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["run_id"]


def poll_terminal(run_id: str, interval: float = 3.0) -> dict:
    start = time.monotonic()
    while True:
        r = requests.get(f"{BASE_URL}/api/runs/{run_id}", timeout=10).json()
        if r["status"] in {"completed", "failed", "cancelled"}:
            r["_elapsed_seconds"] = round(time.monotonic() - start, 1)
            return r
        time.sleep(interval)


def collect_sse(run_id: str) -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/api/runs/{run_id}/events",
        stream=True,
        timeout=120,
    )
    resp.raise_for_status()
    events: list[dict] = []
    buffer = ""
    for chunk in resp.iter_content(chunk_size=None):
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n\n" in buffer:
            block, _, buffer = buffer.partition("\n\n")
            if block.startswith(":") or not block.strip():
                continue
            event: dict = {}
            data_lines: list[str] = []
            for line in block.splitlines():
                if line.startswith("id: "):
                    event["id"] = int(line[4:])
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
            if data_lines:
                event["data"] = json.loads("".join(data_lines))
                events.append(event)
                if event["data"].get("type") in {"run_completed", "run_failed", "run_cancelled"}:
                    # Give the stream a moment to finish, then close.
                    try:
                        resp.close()
                    except Exception:
                        pass
                    return events
    return events


def get_results(run_id: str) -> list[dict]:
    resp = requests.get(f"{BASE_URL}/api/runs/{run_id}/results", timeout=10)
    resp.raise_for_status()
    return resp.json()["results"]


def get_result_detail(run_id: str, result_id: str) -> dict | None:
    resp = requests.get(f"{BASE_URL}/api/runs/{run_id}/results/{result_id}", timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def get_report(run_id: str) -> dict | None:
    resp = requests.get(f"{BASE_URL}/api/runs/{run_id}/report", timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def evidence_summary(run_id: str, results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        detail = get_result_detail(run_id, r["result_id"])
        payload = detail.get("payload") if detail else None
        facts: dict = {}
        if r["tool_name"] == "get_file_layout" and payload:
            facts = {
                "file_count": payload.get("file_count"),
                "total_size_bytes": payload.get("total_size_bytes"),
                "avg_file_size_bytes": payload.get("avg_file_size_bytes"),
                "total_record_count": payload.get("total_record_count"),
            }
        elif r["tool_name"] == "get_partition_spec_usage" and payload:
            facts = {
                "spec_count": len(payload.get("specs", [])),
                "partitioned": any(s.get("fields") for s in payload.get("specs", [])),
            }
        elif r["tool_name"] == "get_sort_order_usage" and payload:
            facts = {
                "files_with_sort_order_id": payload.get("files_with_sort_order_id"),
                "files_without_sort_order_id": payload.get("files_without_sort_order_id"),
            }
        elif r["tool_name"] == "get_delete_file_stats" and payload:
            facts = {
                "delete_file_count": payload.get("delete_file_count"),
                "total_delete_file_size_bytes": payload.get("total_delete_file_size_bytes"),
            }
        elif r["tool_name"] == "get_snapshot_history" and payload:
            facts = {"snapshot_count": len(payload.get("snapshots", []))}
        out.append(
            {
                "result_id": r["result_id"],
                "tool_name": r["tool_name"],
                "row_count": r["row_count"],
                "facts": facts,
            }
        )
    return out


def validate_claims(scenario: str, table: str, report: dict | None, evidence: list[dict]) -> list[dict]:
    findings: list[dict] = []
    facts = SEED_FACTS.get(table, {})

    # Map evidence by tool
    ev_map = {e["tool_name"]: e for e in evidence}

    # Healthy table: should be healthy or only minor issues
    if scenario == "healthy_table":
        file_ev = ev_map.get("get_file_layout", {}).get("facts", {})
        expected_files = facts.get("files", 1)
        if file_ev.get("file_count") != expected_files:
            findings.append(
                {
                    "claim": f"healthy_table has {expected_files} data file(s)",
                    "verdict": "FAIL",
                    "detail": f"evidence shows file_count={file_ev.get('file_count')}",
                }
            )
        else:
            findings.append(
                {
                    "claim": f"healthy_table has {expected_files} data file(s)",
                    "verdict": "PASS",
                    "detail": f"file_count={file_ev.get('file_count')}",
                }
            )
        if report:
            status = report.get("overall_status")
            if status == "healthy":
                findings.append({"claim": "report overall_status is healthy", "verdict": "PASS"})
            else:
                # The seeded table has a sort order defined but the file was written
                # without it, so a minor "healthy" or "needs_attention" issue may be
                # reported; we flag this as a false-positive risk rather than hard fail.
                findings.append(
                    {
                        "claim": "report overall_status is healthy",
                        "verdict": "RISK",
                        "detail": f"overall_status={status!r}; report may flag the sort-order mismatch",
                    }
                )

    # Small files table: should flag small-file problem
    elif scenario == "small_files_table":
        file_ev = ev_map.get("get_file_layout", {}).get("facts", {})
        expected_files = facts.get("files", 100)
        if file_ev.get("file_count") != expected_files:
            findings.append(
                {
                    "claim": f"small_files_table has {expected_files} files",
                    "verdict": "FAIL",
                    "detail": f"file_count={file_ev.get('file_count')}",
                }
            )
        else:
            findings.append(
                {
                    "claim": f"small_files_table has {expected_files} files",
                    "verdict": "PASS",
                    "detail": f"file_count={file_ev.get('file_count')}",
                }
            )
        if report:
            has_small_file_issue = any(
                "small" in issue.get("finding", "").lower()
                or "file" in issue.get("finding", "").lower()
                for issue in report.get("current_issues", [])
            )
            if has_small_file_issue:
                findings.append(
                    {
                        "claim": "report identifies small-files problem",
                        "verdict": "PASS",
                    }
                )
            else:
                findings.append(
                    {
                        "claim": "report identifies small-files problem",
                        "verdict": "FAIL",
                        "detail": "no current_issue mentions small/file in its finding text",
                    }
                )

    # Delete files table: should detect delete files
    elif scenario == "delete_files_table":
        del_ev = ev_map.get("get_delete_file_stats", {}).get("facts", {})
        if del_ev.get("delete_file_count") in (None, 0):
            findings.append(
                {
                    "claim": "delete_files_table has delete files",
                    "verdict": "FAIL",
                    "detail": f"delete_file_count={del_ev.get('delete_file_count')}",
                }
            )
        else:
            findings.append(
                {
                    "claim": "delete_files_table has delete files",
                    "verdict": "PASS",
                    "detail": f"delete_file_count={del_ev.get('delete_file_count')}",
                }
            )
        if report:
            has_delete_issue = any(
                "delete" in issue.get("finding", "").lower()
                for issue in report.get("current_issues", [])
            )
            if has_delete_issue:
                findings.append(
                    {
                        "claim": "report flags delete-file overhead",
                        "verdict": "PASS",
                    }
                )
            else:
                findings.append(
                    {
                        "claim": "report flags delete-file overhead",
                        "verdict": "FAIL",
                        "detail": "no current_issue mentions delete in its finding text",
                    }
                )

    return findings


def run_scenario(label: str, table_name: str) -> dict:
    print(f"\n{'='*60}")
    print(f"Scenario: {label} ({table_name})")
    print(f"{'='*60}")
    run_id = create_run(table_name)
    print(f"run_id={run_id}")

    # Collect terminal status and full SSE stream in parallel-ish (status poll is fast).
    final = poll_terminal(run_id)
    # After terminal, fetch the SSE replay so we see the whole event history.
    events = collect_sse(run_id)

    results = get_results(run_id)
    evidence = evidence_summary(run_id, results)
    report = get_report(run_id)

    print(f"terminal_status={final['status']} elapsed={final['_elapsed_seconds']}s")
    if final.get("error"):
        print(f"error={final['error']}")

    print("\nEvidence (Rxxx):")
    for e in evidence:
        print(f"  {e['result_id']:4s} {e['tool_name']:30s} rows={e['row_count']} facts={e['facts']}")

    print("\nEvent types:")
    event_types = [ev["data"]["type"] for ev in events]
    for i, et in enumerate(event_types, 1):
        print(f"  {i:2d}. {et}")

    print("\nReport overall_status:", report.get("overall_status") if report else "N/A")
    if report:
        for issue in report.get("current_issues", []):
            print(f"  issue: {issue.get('finding')!r} severity={issue.get('severity')} evidence={issue.get('evidence')}")
        for rec in report.get("immediate_remediation", []):
            print(f"  remediation: {rec.get('action')!r}")

    validation = validate_claims(label, table_name, report, evidence)
    print("\nClaim validation:")
    for v in validation:
        print(f"  [{v['verdict']}] {v['claim']}" + (f" — {v.get('detail')}" if v.get("detail") else ""))

    return {
        "label": label,
        "table": table_name,
        "run_id": run_id,
        "final": final,
        "events": event_types,
        "evidence": evidence,
        "report": report,
        "validation": validation,
    }


def main() -> int:
    reports: list[dict] = []
    for label, table in SCENARIOS:
        reports.append(run_scenario(label, table))

    output_path = Path("audit_e2e_results.json")
    output_path.write_text(json.dumps(reports, indent=2, default=str))
    print(f"\nWrote full results to {output_path}")

    # Summary verdict
    print("\n" + "=" * 60)
    print("SUMMARY VERDICT")
    print("=" * 60)
    all_pass = True
    for r in reports:
        status = r["final"]["status"]
        if status != "completed":
            all_pass = False
            print(f"{r['label']}: {status.upper()} — {r['final'].get('error')}")
            continue
        verdicts = [v["verdict"] for v in r["validation"]]
        if "FAIL" in verdicts:
            all_pass = False
        print(f"{r['label']}: completed in {r['final']['_elapsed_seconds']}s — verdicts {verdicts}")
    print("\nOverall:", "PASS" if all_pass else "FAIL")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
