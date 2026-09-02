"""SSE transport for run progress (Runtime_Environments_UI.md #24, #25, #44, #45).

Every persisted event becomes one ordered Server-Sent-Events message:

    id: 17
    data: {"event_id": 17, "run_id": "run_abc", "type": "tool_requested", ...}

- Replay: a reconnecting client sends ``Last-Event-ID``; the API also accepts
  ``after_event_id``. Persisted events are replayed from the ResultStore
  before live tailing continues (no distributed event bus).
- Heartbeats: SSE comment lines keep idle streams alive.
- Termination: the stream ends once the run reached a terminal state and all
  persisted events (including the terminal one) have been sent.
"""

import asyncio
import json
from typing import Any, AsyncIterator

from sta.app.runs import TERMINAL_STATUSES
from sta.results.models import ProgressEvent

# SSE keep-alive and tail-polling defaults. The store is the single source of
# truth: new events are read from SQLite, so nothing is lost between polls.
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
RETRY_HINT_MS = 3000

_HEARTBEAT_COMMENT = ": heartbeat\n\n"


def format_sse_event(event: ProgressEvent) -> str:
    """One ordered SSE message: ``id`` for Last-Event-ID replay plus the full
    envelope as the data payload (Runtime_Environments_UI.md #25)."""
    envelope = event.model_dump(mode="json")
    return (
        f"id: {event.event_id}\n"
        f"data: {json.dumps(envelope, separators=(',', ':'))}\n\n"
    )


def format_sse_comment(text: str) -> str:
    """An SSE comment line; ignored by EventSource clients, keeps
    intermediaries from closing an idle stream."""
    return f": {text}\n\n"


def parse_last_event_id(*candidates: Any) -> int:
    """First usable event id from the given candidates (the Last-Event-ID
    header wins over the query parameter); anything unusable falls back to 0."""
    for candidate in candidates:
        if candidate is None or candidate == "":
            continue
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


async def event_stream(
    store: Any,
    run_id: str,
    after_event_id: int = 0,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """Stream one run's events: persisted replay first, then live tailing.

    Ends after the run is terminal and every persisted event (terminal event
    included) has been delivered. A missing run yields a single error comment
    and ends.
    """
    yield f"retry: {RETRY_HINT_MS}\n\n"
    last_id = max(0, after_event_id)
    last_activity = asyncio.get_running_loop().time()
    while True:
        for event in store.list_events(run_id, after_event_id=last_id):
            yield format_sse_event(event)
            last_id = event.event_id
            last_activity = asyncio.get_running_loop().time()
        run = store.get_run(run_id)
        if run is None:
            yield format_sse_comment("unknown run")
            return
        if run.status in TERMINAL_STATUSES:
            drained = store.list_events(run_id, after_event_id=last_id)
            if not drained:
                return
        now = asyncio.get_running_loop().time()
        if now - last_activity >= heartbeat_interval:
            yield _HEARTBEAT_COMMENT
            last_activity = now
        await asyncio.sleep(poll_interval)