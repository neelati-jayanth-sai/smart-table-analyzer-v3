"""SQLite persistence for runs, query results, reports, and progress events.

Implements the persistence contracts from Architecture.md #17 (Result Store),
#33 (Persistence) and Runtime_Environments_UI.md #25 (Progress Event Contract),
#45 (Event Persistence).

Design notes:

- Synchronous, short operations. Safe to call directly from FastAPI async
  handlers and from multiple threads: every method serializes on a lock around
  one shared connection. This matches the MVP concurrency model of one
  FastAPI process (Runtime_Environments_UI.md #20).
- Result IDs are allocated here and are monotonic per run (R001, R002, ...).
  They are immutable once stored: there is no update or delete path and the
  primary key rejects duplicates.
- Event IDs are monotonic per run, starting at 1, so SSE streams can replay
  persisted events after a reconnect via ``after_event_id``.
- The store measures and remembers only. It never interprets result values.
"""

import json
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sta.results.models import ProgressEvent, QueryResult, RunRecord, utc_now

_RUN_COLUMNS = (
    "run_id",
    "table_name",
    "snapshot_id",
    "started_at",
    "completed_at",
    "status",
    "phase",
    "error",
)
_RUN_UPDATABLE_FIELDS = ("table", "snapshot_id", "completed_at", "status", "phase", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    table_name   TEXT NOT NULL,
    snapshot_id  TEXT,
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    status       TEXT NOT NULL,
    phase        TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS query_results (
    result_id       TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    query_version   TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    snapshot_id     TEXT,
    parameters_json TEXT NOT NULL,
    schema_json     TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    payload_json    TEXT,
    payload_location TEXT,
    duration_ms     INTEGER,
    executed_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, result_id),
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE TABLE IF NOT EXISTS reports (
    run_id      TEXT PRIMARY KEY,
    report_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id          INTEGER NOT NULL,
    run_id            TEXT NOT NULL,
    type              TEXT NOT NULL,
    timestamp         TEXT NOT NULL,
    safe_payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id),
    FOREIGN KEY (run_id) REFERENCES runs (run_id)
);

CREATE INDEX IF NOT EXISTS idx_query_results_run ON query_results (run_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, event_id);
"""


def format_result_id(seq: int) -> str:
    """R001, R002, ... R999, then R1000 without breaking sort order in SQL."""
    return f"R{seq:03d}"


# Reserved run-scoped pseudo-result: the full structural schema persisted at
# run start, before any measurement (Architecture.md #9).
FULL_SCHEMA_RESULT_ID = "R000"


class ResultStore:
    """SQLite-backed store for runs, Rxxx query results, reports, and events."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self._db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ResultStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- runs ---------------------------------------------------------------

    def create_run(self, run: RunRecord) -> RunRecord:
        if not run.run_id:
            raise ValueError("run_id is required")
        with self._lock:
            self._conn.execute(
                f"INSERT INTO runs ({', '.join(_RUN_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_RUN_COLUMNS))})",
                (run.run_id, run.table, run.snapshot_id, run.started_at,
                 run.completed_at, run.status, run.phase, run.error),
            )
            self._conn.commit()
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_RUN_COLUMNS)} FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["table"] = record.pop("table_name")
        return RunRecord(**record)

    def update_run(self, run_id: str, **fields: Any) -> RunRecord | None:
        """Update the given RunRecord fields; returns the updated run or None."""
        unknown = set(fields) - set(_RUN_UPDATABLE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown run fields: {sorted(unknown)}")
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_RUN_COLUMNS)} FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            record["table"] = record.pop("table_name")
            record.update(fields)
            run = RunRecord(**record)
            self._conn.execute(
                "UPDATE runs SET table_name = ?, snapshot_id = ?, completed_at = ?, "
                "status = ?, phase = ?, error = ? WHERE run_id = ?",
                (run.table, run.snapshot_id, run.completed_at, run.status,
                 run.phase, run.error, run.run_id),
            )
            self._conn.commit()
        return run

    # -- query results ------------------------------------------------------

    def store_result(self, result: QueryResult) -> str:
        """Allocate the next per-run Rxxx id, insert immutably, return the id.

        The result_id on the input model is ignored; the allocated id is set
        on the model after a successful insert.
        """
        with self._lock:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM query_results WHERE run_id = ?",
                (result.run_id,),
            ).fetchone()[0]
            result_id = format_result_id(seq)
            self._conn.execute(
                "INSERT INTO query_results ("
                "result_id, run_id, seq, tool_name, query_version, table_name, "
                "snapshot_id, parameters_json, schema_json, row_count, payload_json, "
                "payload_location, duration_ms, executed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (result_id, result.run_id, seq, result.tool_name, result.query_version,
                 result.table, result.snapshot_id,
                 json.dumps(result.parameters, default=str),
                 json.dumps(result.schema, default=str),
                 result.row_count, _dump_json(result.payload),
                 result.payload_location, result.duration_ms, result.executed_at),
            )
            self._conn.commit()
        result.result_id = result_id
        return result_id

    def get_result(self, run_id: str, result_id: str) -> QueryResult | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_id, run_id, tool_name, query_version, table_name, "
                "snapshot_id, parameters_json, schema_json, row_count, payload_json, "
                "payload_location, duration_ms, executed_at "
                "FROM query_results WHERE run_id = ? AND result_id = ?",
                (run_id, result_id),
            ).fetchone()
        return _result_from_row(row) if row is not None else None

    def list_results(self, run_id: str) -> list[QueryResult]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT result_id, run_id, tool_name, query_version, table_name, "
                "snapshot_id, parameters_json, schema_json, row_count, payload_json, "
                "payload_location, duration_ms, executed_at "
                "FROM query_results WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [_result_from_row(row) for row in rows]

    def store_full_schema(
        self,
        run_id: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        tool_name: str,
        query_version: str,
        table: str,
        snapshot_id: str | None = None,
        row_count: int,
    ) -> str:
        """Persist the run's full structural schema as reserved pseudo-result
        R000 (Architecture.md #9). R000 exists exactly once per run, before
        any measurement, so the compact context and the report can reference
        it. ``row_count`` describes the payload size shown in the UI (the
        number of schema fields for a FullSchema record)."""
        body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM query_results WHERE run_id = ? AND result_id = ?",
                (run_id, FULL_SCHEMA_RESULT_ID),
            ).fetchone()
            if exists is not None:
                raise ValueError(
                    f"run {run_id} already has a {FULL_SCHEMA_RESULT_ID} full-schema record"
                )
            self._conn.execute(
                "INSERT INTO query_results ("
                "result_id, run_id, seq, tool_name, query_version, table_name, "
                "snapshot_id, parameters_json, schema_json, row_count, payload_json, "
                "payload_location, duration_ms, executed_at) "
                "VALUES (?, ?, 0, ?, ?, ?, ?, '{}', '{}', ?, ?, NULL, NULL, ?)",
                (FULL_SCHEMA_RESULT_ID, run_id, tool_name, query_version, table,
                 snapshot_id, row_count, json.dumps(body, default=str), utc_now()),
            )
            self._conn.commit()
        self.append_event(
            run_id,
            "result_stored",
            {
                "tool": tool_name,
                "result_id": FULL_SCHEMA_RESULT_ID,
                "row_count": row_count,
                "duration_ms": None,
            },
        )
        return FULL_SCHEMA_RESULT_ID

    # -- reports ------------------------------------------------------------

    def store_report(self, run_id: str, report: dict[str, Any] | BaseModel) -> None:
        payload = report.model_dump(mode="json") if isinstance(report, BaseModel) else report
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO reports (run_id, report_json, created_at) "
                "VALUES (?, ?, ?)",
                (run_id, json.dumps(payload, default=str), utc_now()),
            )
            self._conn.commit()

    def get_report(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT report_json FROM reports WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["report_json"]) if row is not None else None

    # -- progress events ----------------------------------------------------

    def append_event(
        self,
        run_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        timestamp: str | None = None,
    ) -> ProgressEvent:
        """Append one safe progress event; event_id is monotonic per run."""
        with self._lock:
            event_id = self._conn.execute(
                "SELECT COALESCE(MAX(event_id), 0) + 1 FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            event = ProgressEvent(
                event_id=event_id,
                run_id=run_id,
                type=event_type,
                timestamp=timestamp or utc_now(),
                data=data or {},
            )
            self._conn.execute(
                "INSERT INTO events (event_id, run_id, type, timestamp, safe_payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (event.event_id, event.run_id, event.type, event.timestamp,
                 json.dumps(event.data, default=str)),
            )
            self._conn.commit()
        return event

    def list_events(self, run_id: str, after_event_id: int | None = None) -> list[ProgressEvent]:
        """Events for a run ordered by event_id, optionally after a given id."""
        query = (
            "SELECT event_id, run_id, type, timestamp, safe_payload_json "
            "FROM events WHERE run_id = ?"
        )
        params: list[Any] = [run_id]
        if after_event_id is not None:
            query += " AND event_id > ?"
            params.append(after_event_id)
        query += " ORDER BY event_id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            ProgressEvent(
                event_id=row["event_id"],
                run_id=row["run_id"],
                type=row["type"],
                timestamp=row["timestamp"],
                data=json.loads(row["safe_payload_json"]),
            )
            for row in rows
        ]


def _dump_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _result_from_row(row: sqlite3.Row) -> QueryResult:
    return QueryResult(
        result_id=row["result_id"],
        run_id=row["run_id"],
        tool_name=row["tool_name"],
        query_version=row["query_version"],
        table=row["table_name"],
        snapshot_id=row["snapshot_id"],
        parameters=json.loads(row["parameters_json"]),
        schema=json.loads(row["schema_json"]),
        row_count=row["row_count"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] is not None else None,
        payload_location=row["payload_location"],
        duration_ms=row["duration_ms"],
        executed_at=row["executed_at"],
    )