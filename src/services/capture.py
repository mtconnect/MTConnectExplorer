"""Capture service: sessions that collect data from an MTConnect agent into a
dataset file (moved out of src/pages/capture.py)."""

import logging
import time

from pydantic import BaseModel

from mtconnect_explorer_client.schemas import (
    CaptureCreate,
    CaptureSessionInfo,
    SessionPorts,
)

from src.client import (
    MtConnectClientConfiguration,
    MtConnectDataCollector,
    MtConnectDataCollectorConfiguration,
)

from . import datasets
from .errors import Conflict
from .events import session_thread_name
from .hosts import SubprocessHost, ThreadHost, WorkerContext, register_worker  # noqa: F401
from .sessions import STATE_LOCK, Session, SessionManager, new_session_id

logger = logging.getLogger(__name__)


class CaptureWorkerConfig(BaseModel):
    """Self-contained worker input (subprocess-serializable)."""

    create: CaptureCreate
    file_path: str
    session_id: str


def capture_worker(cfg: CaptureWorkerConfig, ctx: WorkerContext) -> None:
    """Run one capture: a MtConnectDataCollector writing one dataset file."""
    create = cfg.create
    collector = MtConnectDataCollector(
        MtConnectDataCollectorConfiguration(
            configuration=MtConnectClientConfiguration(
                base_url=create.base_url,
                timeout=create.timeout_s,
                chunk_size=create.chunk_size,
            ),
            stream_interval_ms=create.interval_ms,
            mode=create.mode,
        ),
        thread_name=session_thread_name(cfg.session_id, "collector"),
    ).start(cfg.file_path)
    try:
        running = False
        seen_alive = False
        while not ctx.stop_event.is_set():
            if collector.is_alive:
                seen_alive = True
                if not running and collector.is_collecting:
                    ctx.set_status("running")
                    running = True
            elif seen_alive:
                # The collector's thread exited on its own (finished or died).
                break
            time.sleep(0.2)
    finally:
        collector.stop()


register_worker("capture", capture_worker, CaptureWorkerConfig)


class CaptureManager(SessionManager):
    scope = "capture"
    worker_kind = "capture"
    info_model = CaptureSessionInfo

    def start(self, create: CaptureCreate) -> Session:
        """Validate atomically, reserve the filename, and launch the worker."""
        with STATE_LOCK:
            self._purge_expired()
            active = {
                s.config.filename
                for s in self.sessions.values()
                if not s.is_terminal and s.config.filename
            }
            if create.filename:
                datasets.validate_name(create.filename)
                if (datasets.base_dir() / create.filename).exists() or create.filename in active:
                    raise Conflict(
                        f"dataset '{create.filename}' already exists",
                        code="dataset_exists",
                    )
                filename = create.filename
            else:
                filename = datasets.generate_capture_filename(create.base_url, active)
            datasets.ensure_dir()

            create = create.model_copy(update={"filename": filename})
            sid = new_session_id(self.scope)
            sess = Session(
                id=sid,
                scope=self.scope,
                label=f"{filename} | {create.base_url}",
                config=create,
                ports=SessionPorts(),  # capture runs no agent and binds no SHDR port
            )
            self.sessions[sid] = sess
            self._spawn(sess, CaptureWorkerConfig(
                create=create,
                file_path=str(datasets.base_dir() / filename),
                session_id=sid,
            ))
            return sess


# Workers run as self-exec subprocesses (own GIL, crash isolation); flip to
# ThreadHost() to run this scope's workers in-process.
capture_manager = CaptureManager(SubprocessHost())
