"""Global event bus (docs/api.md §8).

One process-wide bus: every stored event gets a globally monotonic ``seq`` and
an RFC 3339 ``ts``. Events live in a bounded ring buffer (10,000); ``scope`` /
``session`` / ``level`` are filters over the single stream, not separate
counters. Log records from the ``src`` logger hierarchy are ingested by a
logging handler that recovers the emitting session's id from its thread name
(the ``\\x1f`` scheme — an internal detail of this ingest path).
"""

import logging
import queue
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# A session's worker/collector threads are named "<session-id>\x1f<subname>" so
# their log records can be attributed to the session.
SESSION_THREAD_SEP = "\x1f"

# Session-id prefix -> scope (ids look like cap-1, play-2, sim-3).
_PREFIX_SCOPE = {"cap": "capture", "play": "play", "sim": "simulate"}

_LEVEL_VALUES = logging._nameToLevel


def session_thread_name(session_id: str, subname: str) -> str:
    """Encode a session id into a thread name so its log records can be tagged."""
    return f"{session_id}{SESSION_THREAD_SEP}{subname}"


def session_tag(thread_name: str) -> Optional[str]:
    """Recover the session id encoded in a thread name, or None."""
    if SESSION_THREAD_SEP in (thread_name or ""):
        return thread_name.split(SESSION_THREAD_SEP, 1)[0]
    return None


def scope_of_session(session_id: str) -> Optional[str]:
    """Derive the scope from a session id's prefix (cap-1 -> capture)."""
    prefix = session_id.split("-", 1)[0]
    return _PREFIX_SCOPE.get(prefix)


def rfc3339_now() -> str:
    """RFC 3339 UTC timestamp with millisecond precision: 2026-07-23T18:04:11.234Z"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class EventBus:
    """Thread-safe global event bus with ring buffer, cursors, and subscribers."""

    def __init__(self, capacity: int = 10_000) -> None:
        self._lock = threading.RLock()
        self._buffer: deque = deque(maxlen=capacity)
        self._seq = 0
        self._subscribers: List[queue.Queue] = []

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    # -- publishing ----------------------------------------------------------
    def post(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Assign seq + ts, store the event, and fan it out to subscribers."""
        with self._lock:
            self._seq += 1
            event = {**fields, "seq": self._seq, "ts": rfc3339_now()}
            self._buffer.append(event)
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        return event

    def post_session(
        self,
        scope: str,
        session_id: str,
        status: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Post a session lifecycle (status) event."""
        fields: Dict[str, Any] = {
            "type": "session",
            "scope": scope,
            "session": session_id,
            "event": "status",
            "status": status,
        }
        if detail:
            fields["detail"] = detail
        return self.post(fields)

    # -- querying ------------------------------------------------------------
    @staticmethod
    def _matches(
        event: Dict[str, Any],
        scope: Optional[str],
        session: Optional[str],
        level: Optional[str],
    ) -> bool:
        if scope is not None and event.get("scope") != scope:
            return False
        if session is not None and event.get("session") != session:
            return False
        if level is not None and event.get("type") == "log":
            # Minimum-severity semantics: level=INFO includes WARNING/ERROR.
            floor = _LEVEL_VALUES.get(level.upper(), 0)
            if _LEVEL_VALUES.get(str(event.get("level", "")).upper(), 0) < floor:
                return False
        return True

    def query(
        self,
        after: Optional[int] = None,
        scope: Optional[str] = None,
        session: Optional[str] = None,
        level: Optional[str] = None,
        limit: int = 500,
    ) -> Tuple[List[Dict[str, Any]], int, bool]:
        """Events after the cursor (exclusive), oldest first.

        Returns (events, next_cursor, dropped). ``dropped`` is True when the
        cursor fell off the ring buffer's tail — events were lost to overrun.
        """
        with self._lock:
            oldest_stored = self._seq - len(self._buffer)  # seq before buffer head
            dropped = after is not None and after < oldest_stored
            events: List[Dict[str, Any]] = []
            # A full scan may skip past trailing non-matching events: the
            # counter is global, so a filtered view legitimately skips values.
            next_cursor = self._seq
            for event in self._buffer:
                if after is not None and event["seq"] <= after:
                    continue
                if self._matches(event, scope, session, level):
                    events.append(event)
                    if len(events) >= limit:
                        next_cursor = event["seq"]
                        break
            if after is not None:
                next_cursor = max(next_cursor, after)
            return events, next_cursor, dropped

    # -- streaming -----------------------------------------------------------
    def subscribe(
        self,
        after: Optional[int] = None,
        scope: Optional[str] = None,
        session: Optional[str] = None,
        level: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], bool, queue.Queue]:
        """Atomically backfill from the cursor and register a live subscriber.

        Returns (backfill_events, dropped, live_queue). The queue receives every
        subsequent event (unfiltered — the stream loop applies filters), so no
        event can fall between the backfill and the subscription."""
        with self._lock:
            events, _, dropped = self.query(
                after=after, scope=scope, session=session, level=level,
                limit=len(self._buffer) or 1,
            )
            q: queue.Queue = queue.Queue(maxsize=10_000)
            self._subscribers.append(q)
            return events, dropped, q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


BUS = EventBus()


class _LogIngestHandler(logging.Handler):
    """Feed 'src' log records into the bus, attributing sessions by thread name."""

    def __init__(self, bus: EventBus) -> None:
        super().__init__()
        self._bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tag = session_tag(record.threadName or "")
            scope = scope_of_session(tag) if tag else None
            self._bus.post({
                "type": "log",
                "scope": scope,
                "session": tag if scope else None,
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            })
        except Exception:
            self.handleError(record)


_ingest_installed = False
_ingest_lock = threading.Lock()


def install_log_ingest() -> None:
    """Attach the bus's log-ingest handler to the 'src' logger (idempotent)."""
    global _ingest_installed
    with _ingest_lock:
        if not _ingest_installed:
            logging.getLogger("src").addHandler(_LogIngestHandler(BUS))
            _ingest_installed = True


install_log_ingest()
