"""Play service: sessions that replay a recorded dataset through a bundled
agent + one SHDR adapter per device (worker moved out of src/pages/play.py)."""

import logging
import time
from typing import List, Optional

from pydantic import BaseModel

from mtconnect_explorer_client.schemas import (
    PlayCreate,
    PlaySessionInfo,
    SessionPorts,
)

from src.agent import Agent, AgentAdapter, AgentConfig
from src.converter import MtConnectDatasetPlayer

from . import datasets, ports
from .errors import Conflict
from .events import session_thread_name
from .hosts import SubprocessHost, ThreadHost, WorkerContext, register_worker  # noqa: F401
from .sessions import STATE_LOCK, Session, SessionManager, new_session_id

logger = logging.getLogger(__name__)


class PlayWorkerConfig(BaseModel):
    """Self-contained worker input (subprocess-serializable)."""

    create: PlayCreate
    dataset_path: str
    session_id: str


def play_worker(cfg: PlayWorkerConfig, ctx: WorkerContext) -> None:
    """Run one playback: an agent fronting one SHDR adapter per device, each fed
    by its own MtConnectDatasetPlayer reading the same dataset."""
    _log = logging.getLogger(__name__)
    create = cfg.create
    agent: Optional[Agent] = None
    players: List[MtConnectDatasetPlayer] = []
    try:
        _log.info("starting agent...")

        # One agent fronting one SHDR adapter per selected device.
        agent = Agent(AgentConfig(
            adapters=[
                AgentAdapter(name=d.device_name, host=d.shdr_host, port=d.shdr_port)
                for d in create.devices
            ],
            port=create.agent.port,
            mtconnect_version=create.agent.mtconnect_version,
            asset_buffer_size=create.agent.asset_buffer_size,
            buffer_size=create.agent.buffer_size,
        )).start()

        _log.info("starting %d adapter(s)...", len(create.devices))

        # Each player binds its own SHDR adapter (autoplay deferred) and reads the
        # same dataset file through its own handle, filtered to its device.
        for d in create.devices:
            players.append(MtConnectDatasetPlayer(
                d.shdr_host,
                d.shdr_port,
                create.re_timestamp,
                create.loop,
                thread_name=session_thread_name(
                    cfg.session_id, f"dataset-player-{d.device_name}"
                ),
            ).open(cfg.dataset_path, d.device_name, False))

        _log.info("waiting for agent to connect to all adapters...")
        while not all(p._adapter.agent_is_connected for p in players):
            if ctx.stop_event.is_set():
                _log.info("playback aborted")
                return
            time.sleep(0.1)
        for p in players:
            p.play()
        # The agent has connected to every adapter and data is now streaming.
        ctx.set_status("running")

        _log.info("playback started")

        while not all(p.is_finished for p in players):
            if ctx.stop_event.is_set():
                break
            time.sleep(0.1)

        if not ctx.stop_event.is_set():
            _log.info("playback complete")
        else:
            _log.info("playback stopped")

    finally:
        for p in players:
            try:
                p.stop()
            except Exception:
                pass
        try:
            if agent is not None:
                agent.stop()
        except Exception:
            pass


register_worker("play", play_worker, PlayWorkerConfig)


class PlayManager(SessionManager):
    scope = "play"
    worker_kind = "play"
    info_model = PlaySessionInfo

    def start(self, create: PlayCreate) -> Session:
        """Validate everything atomically (dataset, ports) and launch the worker."""
        with STATE_LOCK:
            self._purge_expired()
            dataset_path = datasets.existing_path(create.dataset)
            if "capture" in datasets.in_use(create.dataset):
                raise Conflict(
                    f"dataset '{create.dataset}' is being captured",
                    code="dataset_in_use",
                )
            ports.validate_ports(
                create.agent.port, [d.shdr_port for d in create.devices]
            )

            sid = new_session_id(self.scope)
            devices = ", ".join(d.device_name for d in create.devices)
            sess = Session(
                id=sid,
                scope=self.scope,
                label=f"{create.dataset} | {devices} | localhost:{create.agent.port}",
                config=create,
                ports=SessionPorts(
                    agent=create.agent.port,
                    shdr=[d.shdr_port for d in create.devices],
                ),
            )
            self.sessions[sid] = sess
            self._spawn(sess, PlayWorkerConfig(
                create=create,
                dataset_path=str(dataset_path),
                session_id=sid,
            ))
            return sess


# Workers run as self-exec subprocesses (own GIL, crash isolation); flip to
# ThreadHost() to run this scope's workers in-process.
play_manager = PlayManager(SubprocessHost())
