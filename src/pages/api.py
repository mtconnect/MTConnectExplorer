"""Frontend → backend wiring.

The Streamlit pages talk to the backend exclusively through the lib's
``ApiClient`` against this process's own API (loopback), authenticating with
the runtime token — the single place the frontend touches ``src.services``.
Pointing the frontend at a remote backend later is a config change here, not a
refactor.

Each scope gets one shared background reader on ``/events/stream`` (a
``ScopeFeed``) that renders backend log events into the page's log pane,
merged with frontend-generated ``[ui]`` lines (e.g. API-call failures).
"""

import threading
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional

from mtconnect_explorer_client import ApiClient
from mtconnect_explorer_client.schemas import SessionInfo

from src.services import auth

_lock = threading.RLock()
_client: Optional[ApiClient] = None


def get_client() -> ApiClient:
    """The process-wide client for this app's own API (thread-safe)."""
    global _client
    with _lock:
        if _client is None:
            _client = ApiClient(
                f"http://127.0.0.1:{auth.server_port()}/api/v1",
                api_key=auth.runtime_token,
            )
        return _client


def _fmt_ts(ts: datetime) -> str:
    """Local-time log prefix matching the classic '%(asctime)s' look."""
    local = ts.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S") + f",{local.microsecond // 1000:03d}"


class ScopeFeed:
    """Background reader of /events/stream for one scope.

    Log events are rendered into ``lines`` tagged with the emitting session's
    label (ids are mapped via ``update_labels``, fed by the page's session
    list). The deque bounds what was an unbounded log list."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.lines: deque = deque(maxlen=2000)
        self.labels: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None

    def ensure_started(self) -> None:
        with _lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name=f"event-feed-{self.scope}", daemon=True
                )
                self._thread.start()

    def update_labels(self, sessions: List[SessionInfo]) -> None:
        for sess in sessions:
            self.labels[sess.id] = sess.label

    def post_ui(self, message: str, level: str = "ERROR") -> None:
        """Append a frontend-generated line (API-call failures etc.)."""
        self.lines.append(f"{_fmt_ts(datetime.now().astimezone())} [{level}] [ui] {message}")

    def _run(self) -> None:
        try:
            for event in get_client().events_stream(scope=self.scope):
                if event.type != "log":
                    continue
                who = self.labels.get(event.session, event.session or "-")
                self.lines.append(
                    f"{_fmt_ts(event.ts)} [{event.level}] [{who}] {event.msg}"
                )
        except Exception as ex:
            self.post_ui(f"event stream for '{self.scope}' stopped: {ex}")


_feeds: Dict[str, ScopeFeed] = {}


def get_feed(scope: str) -> ScopeFeed:
    """The process-wide event feed for a scope (reader thread lazily started)."""
    with _lock:
        feed = _feeds.get(scope)
        if feed is None:
            feed = _feeds[scope] = ScopeFeed(scope)
    feed.ensure_started()
    return feed
