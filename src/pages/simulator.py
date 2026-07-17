import logging
import threading
import time
from typing import List, Optional

import streamlit as st

from src.agent import Agent, AgentConfig, AgentAdapter
from src.simulator import EquipmentType, EquipmentSimulator
from src.viz import render_equipment_html
from src.pages.core import (
    SimulatorConfig,
    SimulatorPageState,
    SimulatorSession,
    agent_url,
    all_used_ports,
    attach_session_log,
    detach_log,
    drain,
    get_simulator_state,
    get_state_lock,
    log_area,
    session_thread_name,
)

_EQUIPMENT_OPTIONS = [EquipmentType.MILL_3AXIS, EquipmentType.ROBOT_6DOF]


def _sim_worker(
    config: SimulatorConfig,
    simulator: EquipmentSimulator,
    stop_event: threading.Event,
    log_label: str,
) -> None:
    """Start the agent + SHDR adapter, run the simulator, and tear both down.

    Mirrors ``_play_worker`` in play.py: the only difference is the data source
    (a generated routine instead of a recorded dataset)."""
    _log = logging.getLogger(__name__)
    agent: Optional[Agent] = None
    try:
        _log.info("starting agent...")
        agent = Agent(AgentConfig(
            adapters=[AgentAdapter(
                name=config.device_name,
                host=config.player_host,
                port=config.player_port,
            )],
            port=config.agent_port,
            mtconnect_version=config.mtconnect_version,
            asset_buffer_size=config.asset_buffer_size,
            buffer_size=config.buffer_size,
        )).start()

        _log.info("starting adapter...")
        simulator.start()  # binds the SHDR adapter's TCP server

        _log.info("waiting for agent to connect to adapter...")
        while not simulator.adapter.agent_is_connected:
            if stop_event.is_set():
                _log.info("simulation aborted")
                return
            time.sleep(0.1)

        simulator.play()
        _log.info("simulation started")

        while not stop_event.is_set():
            if agent is not None and not agent.is_running:
                _log.error("agent stopped unexpectedly")
                break
            time.sleep(0.2)

        _log.info("simulation stopped")

    except Exception as ex:
        _log.error(f"sim worker error: {ex}")
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


def _session_label(equipment: EquipmentType, device_name: str, agent_port: int) -> str:
    """Human-readable session label used to tag log lines and label the grid."""
    return f"{equipment.label} | {device_name} | localhost:{agent_port}"


def _next_free_port(start: int, used: set) -> int:
    p = start
    while p in used:
        p += 1
    return p


def _sync_log_handler(state: SimulatorPageState) -> None:
    """Detach the consolidated log handler once no sessions remain. Caller holds the lock."""
    if not state.sessions and state.handler is not None:
        detach_log(state.handler)
        state.handler = None


def _remove_session(state: SimulatorPageState, sid: str) -> None:
    """Signal a session's worker to stop, wait briefly, and drop it. Caller holds the lock."""
    sess = state.sessions.get(sid)
    if sess is None:
        return
    if sess.stop_event is not None:
        sess.stop_event.set()
    worker = sess.worker
    if worker is not None and worker.is_alive():
        worker.join(timeout=15)
    state.sessions.pop(sid, None)
    _sync_log_handler(state)


def _reap_finished(state: SimulatorPageState) -> None:
    """Drop sessions whose worker exited on its own. Caller holds the lock."""
    for sid, sess in list(state.sessions.items()):
        worker = sess.worker
        if worker is None or not worker.is_alive():
            if worker is not None:
                worker.join(timeout=1)
            state.sessions.pop(sid, None)
    _sync_log_handler(state)


# Widen the dialog to fill most of the browser window (the built-in
# width="large" preset is much narrower). Injected only while the dialog is open.
_DIALOG_CSS = """
<style>
div[data-testid="stDialog"] div[role="dialog"] {
    width: 95vw !important;
    max-width: 95vw !important;
}
</style>
"""


@st.dialog("Simulator", width="large")
def _visualize_dialog(sid: str) -> None:
    """Modal showing the animated 3D model of a running simulation.

    There is intentionally no in-dialog Close button: st.rerun() inside a dialog
    reruns the dialog's own scope and reopens it, so a button cannot close it —
    the dialog's built-in ✕ (top-right) is used to close."""
    st.markdown(_DIALOG_CSS, unsafe_allow_html=True)
    state = get_simulator_state()
    sess = state.sessions.get(sid)
    if sess is None or sess.simulator is None:
        st.warning("This simulation is no longer running. Close this window with ✕.")
        return

    simulator = sess.simulator
    spec = simulator.spec
    # The animation phase-locks to the simulator's Unix start time (start_epoch), which the
    # worker sets only once the agent has booted, connected, and emission begins — a few
    # seconds after "Simulate". Until then there is no data timeline to sync to, so don't
    # render a mislocked animation: embedding a guessed epoch (e.g. time.time()) here made the
    # view open offset by exactly the startup delay if opened immediately. Show a brief notice
    # instead; reopening once it's running locks on exactly.
    start_epoch = simulator.start_epoch
    if start_epoch is None:
        st.info("Simulation is still starting — reopen this view in a moment to see the 3D model.")
        return

    equipment = EquipmentType(sess.config.equipment_type)
    st.caption(
        f"{equipment.label} — {sess.config.device_name} — {sess.config.routine}"
        "  ·  middle-drag: rotate · right-drag: pan · scroll: zoom · motion synced to live data"
    )
    st.iframe(render_equipment_html(spec, start_epoch, height=760), height=780)


@st.fragment(run_every=0.5)
def _log_output_fragment() -> None:
    """Live-refresh the consolidated log via a fragment (partial) rerun rather
    than a full-page rerun, so an open 3D-view dialog is never dismissed by the
    refresh. The 3D view animates on its own (JS) and needs no server refresh."""
    state = get_simulator_state()
    drain(state.log_q, state.log_lines)
    log_area("sim_log_area", state.log_lines)


def _any_session_booting(state: SimulatorPageState) -> bool:
    """True while any session's worker is alive but its simulator has not begun
    emitting (``start_epoch`` unset) — i.e. agent boot/connect is still in progress."""
    return any(
        (s.worker is not None and s.worker.is_alive())
        and s.simulator is not None
        and s.simulator.start_epoch is None
        for s in state.sessions.values()
    )


@st.fragment(run_every=0.5)
def _boot_refresh_fragment() -> None:
    """Mounted only while a session is booting (see the call site). Polls until boot
    completes (or the worker dies), then triggers one full-page rerun so the Now
    Simulating table re-renders and the View button enables. It is not mounted during
    normal operation, so it never dismisses an open 3D-view dialog."""
    if not _any_session_booting(get_simulator_state()):
        st.rerun(scope="app")


def simulator_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Simulate")

    state = get_simulator_state()

    drain(state.log_q, state.log_lines)

    with get_state_lock():
        _reap_finished(state)

    sim_config: SimulatorConfig = state.config

    now_simulating_container = st.container()
    form_container = st.container()

    # --- Now Simulating ---
    if state.sessions:
        with now_simulating_container:
            head_l, head_r = st.columns([3, 1])
            head_l.subheader("Now Simulating")
            with head_r:
                if st.button("Stop All", use_container_width=True):
                    with get_state_lock():
                        for sid in list(state.sessions):
                            _remove_session(state, sid)
                    st.rerun()

            h = st.columns([2, 2, 3, 2, 2, 1, 1])
            h[0].markdown("**Equipment**")
            h[1].markdown("**Device**")
            h[2].markdown("**Agent URL**")
            h[3].markdown("**Data Interval (ms)**")
            h[4].markdown("**Status**")
            h[5].markdown("**View**")
            h[6].markdown("**Stop**")
            for sid, sess in list(state.sessions.items()):
                c = st.columns([2, 2, 3, 2, 2, 1, 1])
                c[0].write(EquipmentType(sess.config.equipment_type).label)
                c[1].write(sess.config.device_name)
                c[2].write(agent_url(sess.config.agent_port))
                c[3].write(str(sess.config.tick_ms))
                alive = sess.worker is not None and sess.worker.is_alive()
                # Ready once emission has begun (start_epoch set): before that there is
                # no data timeline for the 3D view to phase-lock to, so View is disabled.
                ready = (
                    alive
                    and sess.simulator is not None
                    and sess.simulator.start_epoch is not None
                )
                c[4].write(("simulating" if ready else "starting") if alive else "stopping")
                with c[5]:
                    if st.button(
                        "View",
                        key=f"sim_view_{sid}",
                        use_container_width=True,
                        disabled=not ready,
                        help=None if ready else "Available once the simulation has started",
                    ):
                        _visualize_dialog(sid)
                with c[6]:
                    if st.button("Stop", key=f"sim_stop_{sid}", use_container_width=True):
                        with get_state_lock():
                            _remove_session(state, sid)
                        st.rerun()
            st.divider()

            # While any session is still booting, mount the poller that re-renders the
            # page the moment it becomes ready, enabling its View button. Not mounted
            # otherwise, so it never disturbs an open 3D-view dialog.
            if _any_session_booting(state):
                _boot_refresh_fragment()

    # --- Start Simulation (kept narrow; it looks best at this width) ---
    _, form_col, _ = form_container.columns([1, 2, 1])
    with form_col:
        # --- Start Simulating (at the top, like Play's launch settings) ---
        st.subheader("Start Simulating")

        current_equipment = EquipmentType(sim_config.equipment_type)
        equipment: EquipmentType = st.selectbox(
            "Equipment Type",
            _EQUIPMENT_OPTIONS,
            index=_EQUIPMENT_OPTIONS.index(current_equipment),
            format_func=lambda e: e.label,
            key="sim_equipment",
            filter_mode=None,  # Disable search/text input
        )

        # Device name + routine default per equipment (keys embed the equipment
        # value so switching type re-seeds these fields to that type's defaults).
        device_name = st.text_input(
            "Device Name",
            value=equipment.default_device_name,
            key=f"sim_device_name_{equipment.value}",
        )
        routine = st.selectbox(
            "Routine",
            [equipment.routine_name],
            index=0,
            key=f"sim_routine_{equipment.value}",
            filter_mode=None,
        )

        tick_ms = st.number_input(
            "Data Interval (ms)",
            value=int(sim_config.tick_ms), min_value=1, max_value=60000, step=1,
            key="sim_tick_ms",
            help="How often the simulator computes and feeds a new observation to the "
            "SHDR adapter. Lower = smoother motion and a higher data rate.",
        )

        with st.expander("Equipment Configuration"):
            st.caption(
                "No equipment-specific settings yet. "
                + ("Mill axes are fixed at 0–1000 mm." if equipment == EquipmentType.MILL_3AXIS
                   else "Robot joints follow a generic 6-DOF kinematic layout.")
            )

        # Ports must be unique across simultaneously-running sessions (Play +
        # Simulator). Pre-fill the next free ports but let the user override.
        used_ports = all_used_ports()
        suggested_agent = _next_free_port(SimulatorConfig.model_fields["agent_port"].default, used_ports)
        suggested_player = _next_free_port(SimulatorConfig.model_fields["player_port"].default, used_ports)
        gen = state.next_id

        # --- Agent config ---
        st.subheader("Agent Configuration")
        c1, c2 = st.columns(2)
        with c1:
            agent_port = st.number_input(
                "Port", value=suggested_agent, min_value=1, max_value=65535, step=1,
                key=f"sim_agent_port_{gen}",
            )
            mtc_version = st.text_input(
                "MTConnect Version", value=sim_config.mtconnect_version,
                key="sim_mtc_version",
            )
        with c2:
            asset_buf = st.number_input(
                "Asset Buffer Size", value=sim_config.asset_buffer_size, min_value=1, step=1,
                key="sim_asset_buf",
            )
            buf_size = st.number_input(
                "Buffer Size", value=sim_config.buffer_size, min_value=1, step=1,
                key="sim_buf_size",
            )

        # --- SHDR Adapter ---
        st.subheader("SHDR Adapter")
        c1, c2 = st.columns(2)
        with c1:
            player_host = st.text_input(
                "Host", value=sim_config.player_host, key="sim_player_host",
            )
        with c2:
            player_port = st.number_input(
                "Port", value=suggested_player, min_value=1, max_value=65535, step=1,
                key=f"sim_player_port_{gen}",
            )

        # Validate ports against running sessions; disable Simulate until valid.
        agent_port = int(agent_port)
        player_port = int(player_port)
        conflicts: List[str] = []
        if agent_port in used_ports:
            conflicts.append(f"agent port {agent_port} is in use")
        if player_port in used_ports:
            conflicts.append(f"SHDR port {player_port} is in use")
        if agent_port == player_port:
            conflicts.append("agent and SHDR ports must differ")
        ports_valid = not conflicts
        if conflicts:
            st.error("Cannot start: " + "; ".join(conflicts) + ".")

        # --- Simulate ---
        simulate_clicked = st.button(
            "Simulate",
            disabled=not ports_valid or not str(device_name).strip(),
            use_container_width=True, type="primary",
        )

        if simulate_clicked and str(device_name).strip():
            with get_state_lock():
                # Re-validate under the lock — another user may have taken the port.
                used_now = all_used_ports()
                if agent_port in used_now or player_port in used_now or agent_port == player_port:
                    st.warning("Those ports were just taken by another session. Adjust and retry.")
                else:
                    final_config = SimulatorConfig(
                        equipment_type=equipment.value,
                        device_name=str(device_name).strip(),
                        routine=str(routine),
                        tick_ms=int(tick_ms),
                        agent_port=agent_port,
                        mtconnect_version=str(mtc_version),
                        asset_buffer_size=int(asset_buf),
                        buffer_size=int(buf_size),
                        player_host=str(player_host),
                        player_port=player_port,
                    )
                    # Remember non-port settings as defaults for the next launch.
                    state.config = final_config

                    sid = str(state.next_id)
                    state.next_id += 1
                    label = _session_label(equipment, final_config.device_name, agent_port)

                    if state.handler is None:
                        state.handler = attach_session_log(state.log_q, state.sessions)

                    # Build the simulator here (constructor is cheap and binds no
                    # sockets) so the session can hand its routine spec to the
                    # visualization; the worker thread starts/plays/stops it.
                    simulator = EquipmentSimulator(
                        equipment=equipment,
                        device_name=final_config.device_name,
                        host=final_config.player_host,
                        port=final_config.player_port,
                        thread_name=session_thread_name(label, "simulator"),
                        tick_s=final_config.tick_ms / 1000.0,
                    )
                    stop_event = threading.Event()
                    worker = threading.Thread(
                        target=_sim_worker,
                        args=(final_config, simulator, stop_event, label),
                        name=session_thread_name(label, "worker"),
                        daemon=True,
                    )
                    state.sessions[sid] = SimulatorSession(
                        id=sid,
                        label=label,
                        config=final_config,
                        simulator=simulator,
                        worker=worker,
                        stop_event=stop_event,
                    )
                    worker.start()
                    st.rerun()

    # --- Log Output (full width) ---
    st.subheader("Log Output")
    # While sessions run, refresh the log inside a fragment (partial rerun) so a
    # full-page rerun never dismisses an open 3D-view dialog. When idle, render
    # once (the fragment stops running when it is no longer on the page).
    if state.sessions:
        _log_output_fragment()
    else:
        log_area("sim_log_area", state.log_lines)
