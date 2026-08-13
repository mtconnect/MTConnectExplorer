"""Simulate service: sessions that stream a canned routine for one virtual
machine through a bundled agent + SHDR adapter.

Each equipment type has a dedicated create model (the lib's CREATE_MODELS) and
endpoint; configured axis limits (meters/degrees on the wire) are converted to
the simulator's native units (mm/deg) here, scale the routine and 3D geometry,
and are published as MTConnect Constraints in the generated device model.

The routine spec is deterministic per (equipment type, limits), so the manager
computes it at start (for the 3D view / routine endpoint) — the worker never
has to share its live simulator object, which keeps it subprocess-ready. The
one runtime value the view needs, ``start_epoch``, arrives via the worker's
"running" status detail.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple, get_args

from pydantic import BaseModel

from mtconnect_explorer_client.schemas import (
    CREATE_MODELS,
    EquipmentInfo,
    LinearAxisLimits,
    RotaryAxisLimits,
    SessionPorts,
    SimulateCreate,
    SimulateSessionInfo,
)

from src.agent import Agent, AgentAdapter, AgentConfig
from src.simulator import EquipmentSimulator, EquipmentType, build_routine

from . import ports
from .events import session_thread_name
from .hosts import SubprocessHost, ThreadHost, WorkerContext, register_worker  # noqa: F401
from .sessions import STATE_LOCK, Session, SessionManager, new_session_id

logger = logging.getLogger(__name__)


def equipment_catalog() -> List[EquipmentInfo]:
    """The GET /simulate/equipment catalog, derived from the create models."""
    return [
        EquipmentInfo(
            type=key,
            label=EquipmentType(key).label,
            default_device_name=model.model_fields["device_name"].default,
            routines=list(get_args(model.model_fields["routine"].annotation)),
            create_path=f"/api/v1/simulate/start/{key}",
        )
        for key, model in CREATE_MODELS.items()
    ]


def limits_of(create: BaseModel) -> Dict[str, Tuple[float, float]]:
    """Extract the configured axis limits from a create model, converted to the
    simulator's native units: wire meters -> mm for linear axes; degrees pass
    through for rotary axes/joints."""
    limits: Dict[str, Tuple[float, float]] = {}
    for name in type(create).model_fields:
        value = getattr(create, name)
        if isinstance(value, LinearAxisLimits):
            limits[name] = (value.min * 1000.0, value.max * 1000.0)
        elif isinstance(value, RotaryAxisLimits):
            limits[name] = (value.min, value.max)
    return limits


class SimulateWorkerConfig(BaseModel):
    """Self-contained worker input (subprocess-serializable)."""

    create: SimulateCreate
    session_id: str


def simulate_worker(cfg: SimulateWorkerConfig, ctx: WorkerContext) -> None:
    """Start the agent + SHDR adapter, run the simulator, and tear both down.

    Mirrors play_worker: the only difference is the data source (a generated
    routine instead of a recorded dataset)."""
    _log = logging.getLogger(__name__)
    create = cfg.create
    equipment = EquipmentType(create.equipment_type)
    simulator = EquipmentSimulator(
        equipment=equipment,
        device_name=create.device_name,
        host=create.shdr.host,
        port=create.shdr.port,
        thread_name=session_thread_name(cfg.session_id, "simulator"),
        tick_s=create.interval_ms / 1000.0,
        limits=limits_of(create),
    )
    agent: Optional[Agent] = None
    try:
        # Adapter up first, then the agent (see play_worker / the CLAUDE.md gotcha:
        # the bundled agent skips the first-declared adapter if it starts before any
        # adapter is listening). Only one adapter here, so it's not strictly needed,
        # but keeping the order consistent future-proofs a multi-adapter simulate.
        _log.info("starting adapter...")
        simulator.start()  # binds the SHDR adapter's TCP server

        _log.info("starting agent...")
        agent = Agent(AgentConfig(
            adapters=[AgentAdapter(
                name=create.device_name,
                host=create.shdr.host,
                port=create.shdr.port,
            )],
            port=create.agent.port,
            mtconnect_version=create.agent.mtconnect_version,
            asset_buffer_size=create.agent.asset_buffer_size,
            buffer_size=create.agent.buffer_size,
        )).start()

        _log.info("waiting for agent to connect to adapter...")
        while not simulator.adapter.agent_is_connected:
            if ctx.stop_event.is_set():
                _log.info("simulation aborted")
                return
            time.sleep(0.1)

        simulator.play()
        # Emission begins on the loop's first tick; start_epoch is the data
        # timeline's origin the 3D view phase-locks to.
        while simulator.start_epoch is None:
            if ctx.stop_event.is_set():
                _log.info("simulation aborted")
                return
            time.sleep(0.05)
        ctx.set_status("running", detail={"start_epoch": simulator.start_epoch})
        _log.info("simulation started")

        while not ctx.stop_event.is_set():
            if agent is not None and not agent.is_running:
                _log.error("agent stopped unexpectedly")
                break
            time.sleep(0.2)

        _log.info("simulation stopped")

    finally:
        try:
            simulator.stop()
        except Exception:
            pass
        try:
            if agent is not None:
                agent.stop()
        except Exception:
            pass


register_worker("simulate", simulate_worker, SimulateWorkerConfig)


class SimulateManager(SessionManager):
    scope = "simulate"
    worker_kind = "simulate"
    info_model = SimulateSessionInfo

    def start(self, create) -> Session:
        """Validate atomically (ports; the create model already validated the
        equipment parameters) and launch the worker. ``create`` is one of the
        CREATE_MODELS instances — its endpoint determined the type."""
        equipment = EquipmentType(create.equipment_type)
        with STATE_LOCK:
            self._purge_expired()
            ports.validate_ports(create.agent.port, [create.shdr.port])

            sid = new_session_id(self.scope)
            sess = Session(
                id=sid,
                scope=self.scope,
                label=f"{equipment.label} | {create.device_name} | localhost:{create.agent.port}",
                config=create,
                ports=SessionPorts(agent=create.agent.port, shdr=[create.shdr.port]),
            )
            # Deterministic per (equipment, limits); serves the 3D view without
            # reaching into the worker (subprocess-ready).
            sess.spec = build_routine(equipment, limits_of(create))
            self.sessions[sid] = sess
            self._spawn(sess, SimulateWorkerConfig(create=create, session_id=sid))
            return sess

    @staticmethod
    def start_epoch_of(sess: Session) -> Optional[float]:
        """The session's data-timeline origin, once running (else None)."""
        return sess.detail.get("start_epoch")


# Workers run as self-exec subprocesses (own GIL, crash isolation); flip to
# ThreadHost() to run this scope's workers in-process.
simulate_manager = SimulateManager(SubprocessHost())
