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

from typing import Any, Dict

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
        # Bring the SHDR adapters up FIRST, then start the agent. Ordering matters:
        # the bundled cppagent, when started before any adapter is listening,
        # deterministically fails to (re)connect to the *first-declared* adapter on
        # its retry sweep — so with N>1 devices it connects to only N-1, and the
        # wait below would hang forever in 'starting'. (Single-adapter simulate/
        # loopback never hit this, which is why it went unnoticed.) Starting every
        # adapter before the agent's initial connection pass sidesteps the race.
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
                snapshot_interval_s=create.snapshot_interval_s,
            ).open(cfg.dataset_path, d.device_name, False))

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

        _log.info("waiting for agent to connect to all adapters...")
        # Bounded wait with per-adapter diagnostics. If the agent never connects to
        # some adapter, fail with the offending device names rather than spinning in
        # 'starting' indefinitely. The agent retries roughly every 10 s, so allow a
        # few retry intervals before giving up.
        named_players = list(zip((d.device_name for d in create.devices), players))
        connect_deadline = time.time() + 40.0
        last_report = 0.0
        while not all(p._adapter.agent_is_connected for _, p in named_players):
            if ctx.stop_event.is_set():
                _log.info("playback aborted")
                return
            now = time.time()
            waiting = [name for name, p in named_players if not p._adapter.agent_is_connected]
            if now - last_report >= 2.0:
                _log.info(
                    "agent connected to %d/%d adapter(s); still waiting for: %s",
                    len(named_players) - len(waiting), len(named_players), ", ".join(waiting),
                )
                last_report = now
            if now > connect_deadline:
                raise RuntimeError(
                    f"agent connected to only {len(named_players) - len(waiting)}/"
                    f"{len(named_players)} adapter(s); never connected to: "
                    + ", ".join(waiting)
                )
            time.sleep(0.1)
        for _, p in named_players:
            p.play()

        # The playhead is computed client-side from an origin (like the 3D view):
        # pass_start_epoch is the wall time of the current pass's offset 0, so
        # position = now - pass_start_epoch. Publish it on start and whenever it
        # moves (loop-wrap or seek) — a handful of events, not a per-tick feed.
        # players[0] is the reference; all players share one dataset/timeline.
        duration = players[0].duration

        def publish_origin() -> None:
            ctx.set_status("running", detail={
                "duration": duration,
                "pass_start_epoch": players[0].pass_start_epoch,
                "looping": create.loop,
                # paused + frozen position let the scrub bar hold the thumb without
                # a per-tick feed: pass_start_epoch stops advancing while paused, so
                # the UI reads position (not now - epoch) when paused is true.
                "paused": players[0].is_paused,
                "position": players[0].position,
            })

        # The agent has connected to every adapter and data is now streaming.
        publish_origin()
        last_epoch = players[0].pass_start_epoch
        _log.info("playback started")

        while not all(p.is_finished for p in players):
            if ctx.stop_event.is_set():
                break

            # Apply any pending seek commands to every player so they stay
            # synchronized on the shared timeline (latest-wins within a drain).
            cmd = ctx.poll_command()
            while cmd is not None:
                kind = cmd.get("cmd")
                if kind == "seek":
                    target = float(cmd.get("position", 0.0))
                    _log.info("seeking to %.1fs", target)
                    for p in players:
                        p.seek(target)
                elif kind == "pause":
                    _log.info("pausing playback")
                    for p in players:
                        p.pause()
                    publish_origin()  # broadcast paused + frozen position at once
                elif kind == "resume":
                    _log.info("resuming playback")
                    for p in players:
                        p.resume()
                    # The pacing clock rebases in each player thread's barrier; the
                    # epoch-move check below republishes the shifted origin, and this
                    # immediate publish clears the paused flag for the UI.
                    publish_origin()
                    last_epoch = players[0].pass_start_epoch
                cmd = ctx.poll_command()

            # Republish the origin when it moves (a loop-wrap or an applied seek
            # both rebase pass_start_epoch). Small tolerance ignores float noise.
            epoch = players[0].pass_start_epoch
            if epoch is not None and (last_epoch is None or abs(epoch - last_epoch) > 0.05):
                publish_origin()
                last_epoch = epoch

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

    def seek(self, sid: str, position: float) -> None:
        """Seek a running playback to ``position`` seconds. Requires the session
        to be running and in re_timestamp mode (seeking re-emits data, and the
        agent needs non-decreasing timestamps, which only holds when we rewrite
        them). Raises Conflict otherwise."""
        sess = self.get(sid)
        if sess.status != "running":
            raise Conflict(f"play session '{sid}' is not running", code="not_running")
        if not sess.config.re_timestamp:
            raise Conflict(
                "seeking requires re-timestamp mode (recorded-timestamp playback "
                "cannot be re-ordered for the agent)",
                code="seek_unavailable",
            )
        self.send_command(sid, {"cmd": "seek", "position": float(position)})

    def pause(self, sid: str) -> None:
        """Pause a running playback. Unlike seek, this needs no re_timestamp mode
        (pause holds the playhead; it does not rewind or re-order data)."""
        sess = self.get(sid)
        if sess.status != "running":
            raise Conflict(f"play session '{sid}' is not running", code="not_running")
        self.send_command(sid, {"cmd": "pause"})

    def resume(self, sid: str) -> None:
        """Resume a paused playback."""
        sess = self.get(sid)
        if sess.status != "running":
            raise Conflict(f"play session '{sid}' is not running", code="not_running")
        self.send_command(sid, {"cmd": "resume"})

    def progress_of(self, sess: Session) -> Dict[str, Any]:
        """Playhead origin published by the worker (docs: the UI computes the
        live position from pass_start_epoch, except while paused, when it uses the
        frozen position). Values are None until running."""
        return {
            "duration": sess.detail.get("duration"),
            "pass_start_epoch": sess.detail.get("pass_start_epoch"),
            "looping": sess.detail.get("looping", sess.config.loop),
            "paused": sess.detail.get("paused", False),
            "position": sess.detail.get("position"),
        }


# Workers run as self-exec subprocesses (own GIL, crash isolation); flip to
# ThreadHost() to run this scope's workers in-process.
play_manager = PlayManager(SubprocessHost())
