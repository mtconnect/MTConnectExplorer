"""Self-exec worker entry point (``--worker <kind>``; see hosts.SubprocessHost).

Runs the exact same worker function the ThreadHost runs, in its own process:
config arrives as one JSON line on stdin; ``{"cmd": "stop"}`` on stdin (or
stdin EOF — the parent died) requests a graceful stop; SIGTERM does the same
on POSIX. Everything the worker has to say goes to stdout as NDJSON:

    {"type": "log", "level": "INFO", "logger": "src.services.play", "msg": "..."}
    {"type": "status", "status": "running", "error": null, "detail": {...}}
    {"type": "exit", "error": null}

This module must be importable without streamlit (main.py dispatches here
before any streamlit import).
"""

import json
import logging
import queue
import signal
import sys
import threading
from typing import Any, Dict, Optional

from .config import load_config
from .hosts import WORKER_REGISTRY, WorkerContext

_stdout_lock = threading.Lock()


def _emit(record: Dict[str, Any]) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
        sys.stdout.flush()


class _NdjsonLogHandler(logging.Handler):
    """Forward every log record to the parent as an NDJSON stdout line."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _emit({
                "type": "log",
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            })
        except Exception:
            self.handleError(record)


def run_worker(kind: str) -> int:
    """Run one worker to completion. Returns the process exit code."""
    # Import the scope services so they register their worker functions.
    # (These pull in the domain modules — agent/shdr/converter — but never
    # streamlit; the whole point of this entry is a lean, UI-free process.)
    from . import capture, play, simulate  # noqa: F401

    if kind not in WORKER_REGISTRY:
        print(f"unknown worker kind '{kind}'", file=sys.stderr)
        return 2
    fn, config_type = WORKER_REGISTRY[kind]

    # All logging (ours and the domain modules') becomes protocol output.
    root = logging.getLogger()
    root.handlers[:] = [_NdjsonLogHandler()]
    root.setLevel(load_config().get_log_level_value())

    config_line = sys.stdin.readline()
    if not config_line.strip():
        print("no worker config on stdin", file=sys.stderr)
        return 2
    config = config_type.model_validate_json(config_line)

    stop_event = threading.Event()
    commands: "queue.Queue" = queue.Queue()

    def _watch_stdin() -> None:
        # Dispatch inbound stdin commands. {"cmd": "stop"} (or EOF, meaning the
        # parent is gone — orphan protection) stops us; every other command is
        # handed to the worker via its context command queue.
        for line in sys.stdin:
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            if not isinstance(cmd, dict):
                continue
            if cmd.get("cmd") == "stop":
                break
            commands.put(cmd)
        stop_event.set()

    threading.Thread(target=_watch_stdin, name="stdin-watch", daemon=True).start()
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    except (ValueError, OSError):
        pass  # not available on this platform/thread

    def set_status(status: str, error: Optional[str] = None,
                   detail: Optional[Dict[str, Any]] = None) -> None:
        _emit({"type": "status", "status": status, "error": error, "detail": detail})

    error: Optional[str] = None
    try:
        fn(config, WorkerContext(stop_event=stop_event, set_status=set_status, commands=commands))
    except Exception as ex:
        error = str(ex)
        logging.getLogger(__name__).error(f"{kind} worker error: {ex}")
    _emit({"type": "exit", "error": error})
    return 1 if error else 0
