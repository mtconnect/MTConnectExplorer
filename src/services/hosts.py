"""Worker-host seam (docs/api.md §10).

A worker is a pure function ``fn(config: BaseModel, ctx: WorkerContext)``,
registered under a scope kind ("capture" | "play" | "simulate"). Managers start
workers through a ``WorkerHost`` and never touch threads or processes directly,
so a scope can be flipped between in-process threads (``ThreadHost``) and
self-exec subprocesses (``SubprocessHost``) without changing any API behavior.

``SubprocessHost`` re-executes this application with a hidden ``--worker``
flag (``MTConnectExplorer --worker <kind>`` when frozen,
``python -m src --worker <kind>`` in dev — dispatched in main.py before any
streamlit import). Protocol: the worker config is one JSON line on stdin;
further ``{"cmd": ...}`` lines on stdin deliver commands to the running worker —
``{"cmd": "stop"}`` (or stdin EOF — orphan protection) requests a graceful stop,
and any other command (e.g. ``{"cmd": "seek", "position": ...}``) is delivered
to the worker via its context command channel. The child emits NDJSON on stdout
(``status`` / ``log`` / ``exit`` records) which the parent's reader thread
forwards to the manager and the event bus. See src/services/worker_main.py for
the child side.
"""

import json
import logging
import queue
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, Optional, Tuple, Type

from pydantic import BaseModel

from .events import BUS, scope_of_session, session_tag

logger = logging.getLogger(__name__)

# kind -> (worker function, config model type). Populated by the scope
# services at import time; worker_main.py resolves the same registry in the
# child process.
WORKER_REGISTRY: Dict[str, Tuple[Callable[[BaseModel, "WorkerContext"], None], Type[BaseModel]]] = {}


def register_worker(
    kind: str,
    fn: Callable[[BaseModel, "WorkerContext"], None],
    config_type: Type[BaseModel],
) -> None:
    WORKER_REGISTRY[kind] = (fn, config_type)


class WorkerContext:
    """What a worker gets from its manager: a stop signal, a status channel, and
    an inbound command channel."""

    def __init__(
        self,
        stop_event: threading.Event,
        set_status: Callable[..., None],
        commands: Optional["queue.Queue"] = None,
    ) -> None:
        self.stop_event = stop_event
        # set_status(status, *, error=None, detail=None) — reports lifecycle
        # transitions (typically just "running") back to the owning manager.
        self.set_status = set_status
        # Inbound command channel — dicts like {"cmd": "seek", "position": ...}.
        # Unlike the one-shot stop_event, commands carry a payload and repeat.
        # ThreadHost shares this queue with the handle (in-process); Subprocess
        # workers get a child-local queue fed by their stdin reader.
        self.commands: "queue.Queue" = commands if commands is not None else queue.Queue()

    def poll_command(self) -> Optional[Dict[str, Any]]:
        """Return the next pending command, or None (non-blocking)."""
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None


class WorkerHandle:
    """Opaque handle to a started worker."""

    def is_alive(self) -> bool:
        raise NotImplementedError

    def join(self, timeout: Optional[float] = None) -> None:
        raise NotImplementedError

    def send_command(self, cmd: Dict[str, Any]) -> None:
        """Deliver a command dict to the running worker (see WorkerContext.commands)."""
        raise NotImplementedError


class WorkerHost:
    """Executor seam: starts a registered worker and reports its exit."""

    def start(
        self,
        kind: str,
        config: BaseModel,
        ctx: WorkerContext,
        thread_name: str,
        on_exit: Callable[[Optional[str]], None],
    ) -> WorkerHandle:
        """Run the worker for ``kind`` with ``config``. ``on_exit(error)`` is
        called exactly once when the worker ends — error is None on a clean
        return, or the failure message on a crash."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ThreadHost — daemon threads in this process
# ---------------------------------------------------------------------------
class _ThreadHandle(WorkerHandle):
    def __init__(self, thread: threading.Thread, commands: "queue.Queue") -> None:
        self._thread = thread
        self._commands = commands

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)

    def send_command(self, cmd: Dict[str, Any]) -> None:
        # Same in-process queue the worker's ctx polls.
        self._commands.put(cmd)


class ThreadHost(WorkerHost):
    """Runs workers as daemon threads in this process."""

    def start(
        self,
        kind: str,
        config: BaseModel,
        ctx: WorkerContext,
        thread_name: str,
        on_exit: Callable[[Optional[str]], None],
    ) -> WorkerHandle:
        fn, _ = WORKER_REGISTRY[kind]

        def _run() -> None:
            error: Optional[str] = None
            try:
                fn(config, ctx)
            except Exception as ex:
                error = str(ex)
                logger.error(f"{kind} worker error: {ex}")
            finally:
                on_exit(error)

        thread = threading.Thread(target=_run, name=thread_name, daemon=True)
        thread.start()
        return _ThreadHandle(thread, ctx.commands)


# ---------------------------------------------------------------------------
# SubprocessHost — self-exec worker processes (own GIL, crash isolation)
# ---------------------------------------------------------------------------
def _worker_argv(kind: str) -> list:
    if getattr(sys, "frozen", False):  # PyInstaller binary re-enters itself
        return [sys.executable, "--worker", kind]
    return [sys.executable, "-m", "src", "--worker", kind]


class _ProcessHandle(WorkerHandle):
    def __init__(self, proc: subprocess.Popen, stdin_lock: threading.Lock) -> None:
        self._proc = proc
        self._stdin_lock = stdin_lock

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def send_command(self, cmd: Dict[str, Any]) -> None:
        # Deliver as a stdin NDJSON line; the child's stdin reader dispatches it
        # onto the worker's command queue. Serialized against the stop-writer
        # (both target proc.stdin).
        with self._stdin_lock:
            try:
                self._proc.stdin.write(json.dumps(cmd) + "\n")
                self._proc.stdin.flush()
            except Exception:
                pass  # child already gone

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for exit; escalate terminate -> kill rather than leak a
        process subtree (the worker's own finally-teardown gets its chance via
        the graceful stop command sent before join is called)."""
        try:
            self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            logger.warning("worker process %s did not stop in time; terminating", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                logger.error("worker process %s survived terminate; killing", self._proc.pid)
                self._proc.kill()


class SubprocessHost(WorkerHost):
    """Runs each worker as a self-exec subprocess.

    Worth the process: each worker (and its tick loop) gets its own GIL, so a
    busy capture/replay can't add jitter to another session's emission timing,
    and a worker crash can't take the server down. The worker owns its
    ``agent[.exe]`` child and tears it down on stop (agent.py's kill-by-port
    fallback remains the backstop for hard kills)."""

    def start(
        self,
        kind: str,
        config: BaseModel,
        ctx: WorkerContext,
        thread_name: str,
        on_exit: Callable[[Optional[str]], None],
    ) -> WorkerHandle:
        session_id = session_tag(thread_name) or ""
        scope = scope_of_session(session_id) if session_id else None

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            _worker_argv(kind),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=creationflags,
        )
        # All writes to the child's stdin (config, stop, and commands) go through
        # this lock so the stop-writer and send_command can't interleave.
        stdin_lock = threading.Lock()
        # Hand the worker its config as the first stdin line; keep stdin open
        # for commands (and as the orphan-protection lifeline — the child stops
        # itself on stdin EOF if this process dies).
        with stdin_lock:
            proc.stdin.write(config.model_dump_json() + "\n")
            proc.stdin.flush()

        exit_error: Dict[str, Optional[str]] = {"error": None, "seen_exit": None}

        def _post_log(record: Dict[str, Any]) -> None:
            BUS.post({
                "type": "log",
                "scope": scope,
                "session": session_id or None,
                "level": str(record.get("level", "INFO")),
                "logger": str(record.get("logger", kind)),
                "msg": str(record.get("msg", "")),
            })

        def _read_stdout() -> None:
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        _post_log({"level": "INFO", "logger": kind, "msg": line})
                        continue
                    rtype = record.get("type")
                    if rtype == "status":
                        ctx.set_status(
                            record.get("status", ""),
                            error=record.get("error"),
                            detail=record.get("detail"),
                        )
                    elif rtype == "log":
                        _post_log(record)
                    elif rtype == "exit":
                        exit_error["seen_exit"] = "yes"
                        exit_error["error"] = record.get("error")
            finally:
                returncode = proc.wait()
                error = exit_error["error"]
                if exit_error["seen_exit"] is None and returncode != 0:
                    # Died without completing the protocol (crash / hard kill).
                    error = error or f"worker process exited with code {returncode}"
                on_exit(error)

        def _read_stderr() -> None:
            # Non-protocol output (interpreter crashes, native noise). Route it
            # through the parent's logger; this thread's name carries the
            # session id, so the bus ingest attributes the lines correctly.
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    logger.error(f"[{kind} worker] {line}")

        def _watch_stop() -> None:
            # Bridge the manager's in-process stop_event across the process
            # boundary as the graceful {"cmd": "stop"} command.
            ctx.stop_event.wait()
            with stdin_lock:
                try:
                    proc.stdin.write('{"cmd": "stop"}\n')
                    proc.stdin.flush()
                except Exception:
                    pass  # child already gone

        threading.Thread(
            target=_read_stdout, name=thread_name, daemon=True
        ).start()
        threading.Thread(
            target=_read_stderr,
            name=session_thread_name_for(session_id, "worker-stderr"),
            daemon=True,
        ).start()
        threading.Thread(
            target=_watch_stop,
            name=session_thread_name_for(session_id, "worker-stop"),
            daemon=True,
        ).start()
        return _ProcessHandle(proc, stdin_lock)


def session_thread_name_for(session_id: str, subname: str) -> str:
    from .events import session_thread_name

    return session_thread_name(session_id, subname) if session_id else subname
