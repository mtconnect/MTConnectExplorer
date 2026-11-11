"""Session base: the runtime Session object, the shared state lock, the global
session-id allocator, the manager registry, and the SessionManager base class
implementing the common lifecycle (docs/api.md §5).

One lock guards all session mutation across every scope so port
validate-and-reserve is atomic app-wide. Blocking joins happen OUTSIDE the
lock; only bookkeeping happens under it.
"""

import itertools
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel

from mtconnect_explorer_client.schemas import (
    TERMINAL_STATUSES,
    SessionInfo,
    SessionPorts,
)

from .errors import NotFound
from .events import BUS, session_thread_name
from .hosts import WorkerContext, WorkerHandle, WorkerHost

logger = logging.getLogger(__name__)

# One reentrant lock for ALL session state (every scope) — port reservation
# must be atomic across scopes, and Streamlit page Start/Stop sections also
# serialize on it.
STATE_LOCK = threading.RLock()

# Terminal sessions are retained (for plain request/response clients) until
# DELETE or this auto-purge deadline.
PURGE_AFTER_S = 15 * 60

SCOPE_PREFIX = {"capture": "cap", "play": "play", "simulate": "sim"}

# One global counter across all scopes: cap-1, play-2, sim-3, ...
_id_counter = itertools.count(1)


def new_session_id(scope: str) -> str:
    return f"{SCOPE_PREFIX[scope]}-{next(_id_counter)}"


class Session:
    """Runtime session record. The wire envelope is produced by to_info()."""

    def __init__(
        self,
        id: str,
        scope: str,
        label: str,
        config: BaseModel,
        ports: SessionPorts,
    ) -> None:
        self.id = id
        self.scope = scope
        self.label = label
        self.config = config
        self.ports = ports
        self.status: str = "starting"
        self.created_at: datetime = datetime.now(timezone.utc)
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.error: Optional[str] = None
        self.detail: Dict[str, Any] = {}
        self.stop_event = threading.Event()
        self.handle: Optional[WorkerHandle] = None
        # Scope extras (e.g. simulate's routine spec) are plain attributes set
        # by the owning manager.

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_info(self, model: Type[SessionInfo] = SessionInfo) -> SessionInfo:
        return model(
            id=self.id,
            scope=self.scope,
            label=self.label,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            error=self.error,
            ports=self.ports,
            config=self.config if model is not SessionInfo else self.config.model_dump(),
        )


# scope -> manager, so cross-cutting views (ports, agents, dataset in-use) can
# be derived from the live session tables without import cycles.
_managers: Dict[str, "SessionManager"] = {}


def register_manager(manager: "SessionManager") -> None:
    _managers[manager.scope] = manager


def managers() -> Dict[str, "SessionManager"]:
    return _managers


def all_live_sessions() -> List[Session]:
    """Every non-terminal session across all scopes."""
    out: List[Session] = []
    for manager in _managers.values():
        with STATE_LOCK:
            out.extend(s for s in manager.sessions.values() if not s.is_terminal)
    return out


class SessionManager:
    """Common lifecycle for a scope's sessions. Subclasses implement start()."""

    scope: str = ""
    worker_kind: str = ""
    info_model: Type[SessionInfo] = SessionInfo

    def __init__(self, host: WorkerHost) -> None:
        self.host = host
        self.sessions: Dict[str, Session] = {}
        register_manager(self)

    # -- helpers -------------------------------------------------------------
    def _purge_expired(self) -> None:
        """Drop terminal sessions past the retention deadline. Caller holds the lock."""
        now = time.time()
        for sid, sess in list(self.sessions.items()):
            if sess.is_terminal and sess.finished_at is not None:
                if now - sess.finished_at.timestamp() > PURGE_AFTER_S:
                    self.sessions.pop(sid, None)

    def _set_status(
        self,
        sid: str,
        status: str,
        error: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Transition a session's status and post the event (idempotent-safe)."""
        with STATE_LOCK:
            sess = self.sessions.get(sid)
            if sess is None or sess.is_terminal:
                return
            sess.status = status
            if status == "running" and sess.started_at is None:
                sess.started_at = datetime.now(timezone.utc)
            if status in TERMINAL_STATUSES:
                sess.finished_at = datetime.now(timezone.utc)
                sess.error = error
            if detail:
                sess.detail.update(detail)
        BUS.post_session(self.scope, sid, status, detail=detail)

    def _spawn(self, sess: Session, worker_config: BaseModel) -> None:
        """Start the session's worker via the host. Caller holds the lock."""
        sid = sess.id

        def set_status(status: str, error: Optional[str] = None,
                       detail: Optional[Dict[str, Any]] = None) -> None:
            self._set_status(sid, status, error=error, detail=detail)

        def on_exit(error: Optional[str]) -> None:
            self._set_status(
                sid,
                "error" if error else "finished",
                error=error,
            )

        ctx = WorkerContext(stop_event=sess.stop_event, set_status=set_status)
        sess.handle = self.host.start(
            self.worker_kind,
            worker_config,
            ctx,
            thread_name=session_thread_name(sid, "worker"),
            on_exit=on_exit,
        )
        BUS.post_session(self.scope, sid, "starting")

    # -- queries -------------------------------------------------------------
    def list(self) -> List[Session]:
        with STATE_LOCK:
            self._purge_expired()
            return list(self.sessions.values())

    def get(self, sid: str) -> Session:
        with STATE_LOCK:
            self._purge_expired()
            sess = self.sessions.get(sid)
            if sess is None:
                raise NotFound(f"unknown {self.scope} session '{sid}'",
                               code="session_not_found")
            return sess

    def list_info(self) -> List[SessionInfo]:
        return [s.to_info(self.info_model) for s in self.list()]

    def get_info(self, sid: str) -> SessionInfo:
        return self.get(sid).to_info(self.info_model)

    def send_command(self, sid: str, cmd: Dict[str, Any]) -> None:
        """Deliver a command dict to a running session's worker (e.g. a seek).
        Raises NotFound for an unknown/purged session."""
        sess = self.get(sid)
        if sess.handle is not None:
            sess.handle.send_command(cmd)

    # -- lifecycle -----------------------------------------------------------
    def delete(self, sid: str, join_timeout: float = 15.0) -> None:
        """Stop (signal, join with timeout) and remove the session."""
        with STATE_LOCK:
            sess = self.sessions.get(sid)
            if sess is None:
                raise NotFound(f"unknown {self.scope} session '{sid}'",
                               code="session_not_found")
            if sess.is_terminal:
                self.sessions.pop(sid, None)
                return
            sess.status = "stopping"
        BUS.post_session(self.scope, sid, "stopping")
        sess.stop_event.set()
        if sess.handle is not None and sess.handle.is_alive():
            # Join OUTSIDE the lock — a slow worker must not block other pages
            # or API calls. The worker's on_exit posts finished/error.
            sess.handle.join(timeout=join_timeout)
        with STATE_LOCK:
            self.sessions.pop(sid, None)

    def delete_all(self) -> None:
        for sid in list(self.sessions):
            try:
                self.delete(sid)
            except NotFound:
                pass
