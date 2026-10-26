"""NDJSON event-stream iterator (docs/api.md §8).

``iter_events`` consumes ``GET /events/stream``: it backfills after the cursor,
stays live, and transparently reconnects from the last seen ``seq`` (pings
count — their seq is a cursor checkpoint). Unknown event types and malformed
lines are skipped (forward compatibility).
"""

import json
from typing import Any, Dict, Iterator, Optional, Union

import httpx
from pydantic import TypeAdapter

from .schemas import Event, LogEvent, PingEvent, SessionEvent

AnyEvent = Union[LogEvent, SessionEvent, PingEvent]

_EVENT_ADAPTER: TypeAdapter = TypeAdapter(Event)

# The server pings every 15 s of idle time; the read timeout must sit above
# that so a quiet-but-alive stream is not treated as dead.
STREAM_READ_TIMEOUT_S = 45.0


def parse_event(line: str) -> Optional[AnyEvent]:
    """Parse one NDJSON line into an event model, or None if unknown/invalid."""
    line = line.strip()
    if not line:
        return None
    try:
        return _EVENT_ADAPTER.validate_json(line)
    except Exception:
        return None


def iter_events(
    client: httpx.Client,
    *,
    after: Optional[int] = None,
    scope: Optional[str] = None,
    session: Optional[str] = None,
    level: Optional[str] = None,
    reconnect: bool = True,
) -> Iterator[AnyEvent]:
    """Yield events (including pings) from /events/stream, forever.

    On a dropped connection, reconnects with ``after=<last seen seq>`` so no
    buffered event is missed. Stop by breaking out of the loop / closing the
    client."""
    cursor = after
    while True:
        params: Dict[str, Any] = {}
        if cursor is not None:
            params["after"] = cursor
        if scope is not None:
            params["scope"] = scope
        if session is not None:
            params["session"] = session
        if level is not None:
            params["level"] = level
        try:
            with client.stream(
                "GET",
                "/events/stream",
                params=params,
                # identity: a gzip middleware/proxy would buffer the compressed
                # stream, holding lines back indefinitely on a quiet feed.
                headers={"Accept-Encoding": "identity"},
                timeout=httpx.Timeout(10.0, read=STREAM_READ_TIMEOUT_S),
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    from .exceptions import ApiError

                    raise ApiError.from_response(response)
                for line in response.iter_lines():
                    event = parse_event(line)
                    if event is None:
                        continue
                    cursor = event.seq
                    yield event
        except (httpx.TransportError, httpx.TimeoutException):
            if not reconnect:
                return
            continue
        # Server closed the stream cleanly; reconnect from the cursor.
        if not reconnect:
            return
