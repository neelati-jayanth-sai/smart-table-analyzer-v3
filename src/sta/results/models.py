from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    return f"run_{uuid4().hex[:12]}"


class QueryResult(BaseModel):
    # result_id is allocated by ResultStore.store_result, not by callers.
    result_id: str = ""
    run_id: str
    tool_name: str
    query_version: str
    table: str
    snapshot_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    schema: dict[str, str] = Field(default_factory=dict)
    row_count: int = 0
    payload: Any = None
    payload_location: str | None = None
    duration_ms: int | None = None
    executed_at: str = Field(default_factory=utc_now)


class RunRecord(BaseModel):
    run_id: str
    table: str
    snapshot_id: str | None = None
    started_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None
    status: str = "starting"
    phase: str | None = None
    error: str | None = None


class ProgressEvent(BaseModel):
    event_id: int
    run_id: str
    type: str
    timestamp: str = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)
