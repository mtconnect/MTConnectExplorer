"""Event endpoints (docs/api.md §8): cursor polling and NDJSON streaming."""

import json
import queue
import time
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from mtconnect_explorer_client.schemas import EventPage

from src.services.events import BUS, rfc3339_now

from ..security import require_api_key

router = APIRouter(
    prefix="/events",
    tags=["Events"],
    dependencies=[Depends(require_api_key)],
)

PING_INTERVAL_S = 15.0

_SCOPE_QUERY = Query(None, description="Filter: capture | play | simulate.")
_SESSION_QUERY = Query(None, description="Filter: session id, e.g. play-3.")
_LEVEL_QUERY = Query(
    None, description="Filter log events to this minimum level (e.g. INFO)."
)
_AFTER_QUERY = Query(None, ge=0, description="Exclusive seq cursor to resume from.")


@router.get(
    "",
    summary="Poll events after a seq cursor",
    response_model=EventPage,
)
def poll(
    after: Optional[int] = _AFTER_QUERY,
    scope: Optional[str] = _SCOPE_QUERY,
    session: Optional[str] = _SESSION_QUERY,
    level: Optional[str] = _LEVEL_QUERY,
    limit: int = Query(500, ge=1, le=10_000, description="Maximum events to return."),
) -> EventPage:
    events, next_cursor, dropped = BUS.query(
        after=after, scope=scope, session=session, level=level, limit=limit
    )
    return EventPage(events=events, next=next_cursor, dropped=dropped)


def _ndjson(event: dict) -> str:
    return json.dumps(event, separators=(",", ":")) + "\n"


def _event_stream(
    after: Optional[int],
    scope: Optional[str],
    session: Optional[str],
    level: Optional[str],
) -> Iterator[str]:
    """Backfill after the cursor, then stay live. A sync generator — Starlette
    runs it on the threadpool, so the blocking queue.get needs no asyncio
    bridging. Pings fire every 15 s of idle time and carry the checkpoint seq
    ("you are current through N"); they are never stored in the buffer."""
    backfill, _dropped, live = BUS.subscribe(
        after=after, scope=scope, session=session, level=level
    )
    try:
        checkpoint = after or 0
        last_emit = time.monotonic()
        for event in backfill:
            checkpoint = event["seq"]
            yield _ndjson(event)
            last_emit = time.monotonic()
        while True:
            try:
                event = live.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - last_emit >= PING_INTERVAL_S:
                    yield _ndjson({"type": "ping", "seq": checkpoint, "ts": rfc3339_now()})
                    last_emit = time.monotonic()
                continue
            # The live queue is unfiltered; skipped events still advance the
            # checkpoint — under this filter they would never be delivered.
            checkpoint = event["seq"]
            if BUS._matches(event, scope, session, level):
                yield _ndjson(event)
                last_emit = time.monotonic()
    finally:
        BUS.unsubscribe(live)


@router.get(
    "/stream",
    summary="Stream events as NDJSON (backfill after cursor, then live)",
)
def stream(
    after: Optional[int] = _AFTER_QUERY,
    scope: Optional[str] = _SCOPE_QUERY,
    session: Optional[str] = _SESSION_QUERY,
    level: Optional[str] = _LEVEL_QUERY,
) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(after, scope, session, level),
        media_type="application/x-ndjson",
    )
