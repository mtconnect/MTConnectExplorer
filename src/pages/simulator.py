from typing import Dict, List, Tuple

import streamlit as st

from mtconnect_explorer_client import ApiError
from mtconnect_explorer_client.schemas import (
    CREATE_MODELS,
    AgentSettings,
    EquipmentInfo,
    LinearAxisLimits,
    RotaryAxisLimits,
    ShdrSettings,
)

from src.viz import render_equipment_html
from src.pages.api import get_client, get_feed
from src.pages.core import (
    SimulatorConfig,
    agent_url,
    get_simulator_state,
    get_state_lock,
    log_area,
)

# Map equipment-type values from older sessions to their current key. The
# shared state store (and per-session widget state) outlives code reloads, so a
# value saved before a type was renamed can linger; without this the launch
# form would fall back silently rather than keeping the user's selection.
_LEGACY_EQUIPMENT_ALIASES = {
    "MILL_3AXIS": "mill_3axis",
    "MILL_5AXIS": "mill_5axis_trunnion_bc",
    "MILL_5AXIS_TRUNNION": "mill_5axis_trunnion_bc",
    "ROBOT_6DOF": "robot_6dof",
}

_EQUIPMENT_NOTES = {
    "mill_3axis": "The routine and 3D scene scale into the configured travel.",
    "mill_5axis_trunnion_bc": ("Head carries X/Y/Z; the trunnion table tilts about Y (B) "
                               "and rotates about Z (C, continuous — no limits). "
                               "The routine and 3D scene scale into the configured travel."),
    "robot_6dof": ("Joint limits scale the pick-and-place motion; link lengths are "
                   "fixed physical structure."),
}


def _coerce_equipment(value, options: List[EquipmentInfo]) -> EquipmentInfo:
    """Resolve a stored equipment-type key to a catalog entry, remapping known
    legacy values and otherwise falling back to the first option — so a stale
    value never crashes the page."""
    key = _LEGACY_EQUIPMENT_ALIASES.get(str(value), str(value))
    for entry in options:
        if entry.type == key:
            return entry
    return options[0]


def _equipment_label(type_key: str, options: List[EquipmentInfo]) -> str:
    for entry in options:
        if entry.type == type_key:
            return entry.label
    return type_key


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
    client = get_client()
    try:
        sess = client.simulate_get(sid)
        routine = client.simulate_routine(sid)
    except ApiError:
        st.warning("This simulation is no longer running. Close this window with ✕.")
        return

    # The animation phase-locks to the simulator's Unix start time (start_epoch), which the
    # worker reports only once the agent has booted, connected, and emission begins — a few
    # seconds after "Simulate". Until then there is no data timeline to sync to, so don't
    # render a mislocked animation: embedding a guessed epoch (e.g. time.time()) here made the
    # view open offset by exactly the startup delay if opened immediately. Show a brief notice
    # instead; reopening once it's running locks on exactly.
    if routine.start_epoch is None:
        st.info("Simulation is still starting — reopen this view in a moment to see the 3D model.")
        return

    try:
        options = client.simulate_equipment()
        label = _equipment_label(sess.config.equipment_type, options)
    except ApiError:
        label = sess.config.equipment_type
    st.caption(
        f"{label} — {sess.config.device_name} — {sess.config.routine}"
        "  ·  middle-drag: rotate · right-drag: pan · scroll: zoom · motion synced to live data"
    )
    st.iframe(
        render_equipment_html(
            routine.spec,
            routine.start_epoch,
            server_time=routine.server_time,
            height=760,
        ),
        height=780,
    )


@st.fragment(run_every=0.5)
def _log_output_fragment() -> None:
    """Live-refresh the consolidated log via a fragment (partial) rerun rather
    than a full-page rerun, so an open 3D-view dialog is never dismissed by the
    refresh. The 3D view animates on its own (JS) and needs no server refresh."""
    log_area("sim_log_area", get_feed("simulate").lines)


def _backend_fingerprint() -> tuple:
    """What this page's grid renders from the backend: simulate sessions
    (id, status). Deliberately narrow — a change-triggered full rerun dismisses
    an open 3D-view dialog, so only this page's own sessions may trigger one
    (port suggestions may go momentarily stale; the backend re-validates on
    submit either way)."""
    return tuple(sorted((s.id, s.status) for s in get_client().simulate_list()))


@st.fragment(run_every=0.5)
def _watch_backend_fragment() -> None:
    """The API can change sessions behind this page's back — poll the backend
    and trigger a full-page rerun only when the session table actually changed
    (an API start/stop, or a booting session reaching 'running', which enables
    its View button). A quiet backend never disturbs an open 3D-view dialog."""
    try:
        current = _backend_fingerprint()
    except ApiError:
        return
    if current != st.session_state.get("sim_backend_fp"):
        st.rerun(scope="app")


def simulator_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Simulate")

    client = get_client()
    feed = get_feed("simulate")
    state = get_simulator_state()

    # Auto-delete terminal sessions (finished/error) so the grid reaps
    # instantly, exactly like the pre-API behavior.
    sessions = []
    equipment_options: List[EquipmentInfo] = []
    try:
        equipment_options = client.simulate_equipment()
        for sess in client.simulate_list():
            if sess.status in ("finished", "error"):
                try:
                    client.simulate_stop(sess.id)
                except ApiError:
                    pass
            else:
                sessions.append(sess)
    except ApiError as ex:
        st.error(f"API error: {ex.message}")
    feed.update_labels(sessions)
    if not equipment_options:
        st.stop()
    # Snapshot what this render is based on; the watcher fragment reruns the
    # page when the backend diverges from it (e.g. API-driven start/stop).
    try:
        st.session_state["sim_backend_fp"] = _backend_fingerprint()
    except ApiError:
        pass

    sim_config: SimulatorConfig = state.config

    now_simulating_container = st.container()
    form_container = st.container()

    # --- Now Simulating ---
    if sessions:
        with now_simulating_container:
            head_l, head_r = st.columns([3, 1])
            head_l.subheader("Now Simulating")
            with head_r:
                if st.button("Stop All", width="stretch"):
                    try:
                        client.simulate_stop_all()
                    except ApiError as ex:
                        feed.post_ui(f"stop all failed: {ex.message}")
                    st.rerun()

            h = st.columns([2, 2, 3, 2, 2, 1, 1])
            h[0].markdown("**Equipment**")
            h[1].markdown("**Device**")
            h[2].markdown("**Agent URL**")
            h[3].markdown("**Data Interval (ms)**")
            h[4].markdown("**Status**")
            h[5].markdown("**View**")
            h[6].markdown("**Stop**")
            for sess in sessions:
                c = st.columns([2, 2, 3, 2, 2, 1, 1])
                c[0].write(_equipment_label(sess.config.equipment_type, equipment_options))
                c[1].write(sess.config.device_name)
                c[2].write(agent_url(sess.config.agent.port))
                c[3].write(str(sess.config.interval_ms))
                # Ready once emission has begun (status 'running', which also
                # publishes start_epoch): before that there is no data timeline
                # for the 3D view to phase-lock to, so View is disabled.
                ready = sess.status == "running"
                if sess.status == "running":
                    status = "simulating"
                elif sess.status == "starting":
                    status = "starting"
                else:
                    status = "stopping"
                c[4].write(status)
                with c[5]:
                    if st.button(
                        "View",
                        key=f"sim_view_{sess.id}",
                        width="stretch",
                        disabled=not ready,
                        help=None if ready else "Available once the simulation has started",
                    ):
                        _visualize_dialog(sess.id)
                with c[6]:
                    if st.button("Stop", key=f"sim_stop_{sess.id}", width="stretch"):
                        try:
                            client.simulate_stop(sess.id)
                        except ApiError as ex:
                            feed.post_ui(f"stop failed: {ex.message}")
                        st.rerun()
            st.divider()

    # --- Start Simulation (kept narrow; it looks best at this width) ---
    _, form_col, _ = form_container.columns([1, 2, 1])
    with form_col:
        # --- Start Simulating (at the top, like Play's launch settings) ---
        st.subheader("Start Simulating")

        current_equipment = _coerce_equipment(sim_config.equipment_type, equipment_options)
        # Repair a stale/renamed stored value so it isn't re-coerced every rerun.
        sim_config.equipment_type = current_equipment.type
        # The selectbox's own widget state also survives reloads; drop a value
        # left by an older code version so it re-seeds from `index` instead of
        # raising "the default value is not part of the options".
        if st.session_state.get("sim_equipment") not in equipment_options:
            st.session_state.pop("sim_equipment", None)
        equipment: EquipmentInfo = st.selectbox(
            "Equipment Type",
            equipment_options,
            index=equipment_options.index(current_equipment),
            format_func=lambda e: e.label,
            key="sim_equipment",
            filter_mode=None,  # Disable search/text input
        )

        # Device name + routine default per equipment (keys embed the equipment
        # value so switching type re-seeds these fields to that type's defaults).
        device_name = st.text_input(
            "Device Name",
            value=equipment.default_device_name,
            key=f"sim_device_name_{equipment.type}",
        )
        routine = st.selectbox(
            "Routine",
            equipment.routines,
            index=0,
            key=f"sim_routine_{equipment.type}",
            filter_mode=None,
        )

        interval_ms = st.number_input(
            "Data Interval (ms)",
            value=int(sim_config.interval_ms), min_value=1, max_value=60000, step=1,
            key="sim_interval_ms",
            help="How often the simulator computes and feeds a new observation to the "
            "SHDR adapter. Lower = smoother motion and a higher data rate.",
        )

        # --- Equipment configuration: per-axis limits, introspected from the
        # equipment type's create model so new types get a form for free.
        # Linear axes are meters; rotary axes/joints are degrees. Defaults are
        # the native travel; the routine scales into whatever is configured.
        create_model = CREATE_MODELS[equipment.type]
        limit_values: Dict[str, Tuple[float, float]] = {}
        limit_conflicts: List[str] = []
        with st.expander("Equipment Configuration"):
            st.caption(_EQUIPMENT_NOTES.get(equipment.type, ""))
            for field_name, field_info in create_model.model_fields.items():
                default = field_info.default
                if isinstance(default, LinearAxisLimits):
                    unit, step, fmt = "m", 0.05, "%.3f"
                elif isinstance(default, RotaryAxisLimits):
                    unit, step, fmt = "deg", 5.0, "%.1f"
                else:
                    continue
                c1, c2 = st.columns(2)
                lo = c1.number_input(
                    f"{field_name.upper()} min ({unit})",
                    value=float(default.min), step=step, format=fmt,
                    key=f"sim_lim_{equipment.type}_{field_name}_min",
                )
                hi = c2.number_input(
                    f"{field_name.upper()} max ({unit})",
                    value=float(default.max), step=step, format=fmt,
                    key=f"sim_lim_{equipment.type}_{field_name}_max",
                )
                if not hi > lo:
                    limit_conflicts.append(
                        f"{field_name.upper()}: max ({hi:g} {unit}) must be greater than min ({lo:g} {unit})"
                    )
                limit_values[field_name] = (float(lo), float(hi))
        if limit_conflicts:
            st.error("Invalid axis limits: " + "; ".join(limit_conflicts) + ".")

        # Ports must be unique across simultaneously-running sessions (Play +
        # Simulator). Pre-fill the next free ports (backend suggestions) but let
        # the user override.
        try:
            ports_info = client.ports(shdr_count=1)
            used_ports = set(ports_info.used)
            suggested_agent = ports_info.suggested.agent
            suggested_player = ports_info.suggested.shdr[0]
        except ApiError:
            used_ports = set()
            suggested_agent = SimulatorConfig.model_fields["agent_port"].default
            suggested_player = SimulatorConfig.model_fields["player_port"].default
        gen = state.gen

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

        # Validate ports client-side for instant feedback; the backend
        # re-validates atomically on POST and is the real gate.
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
            disabled=not ports_valid or bool(limit_conflicts) or not str(device_name).strip(),
            width="stretch", type="primary",
        )

        if simulate_clicked and str(device_name).strip() and not limit_conflicts:
            final_config = SimulatorConfig(
                equipment_type=equipment.type,
                device_name=str(device_name).strip(),
                routine=str(routine),
                interval_ms=int(interval_ms),
                agent_port=agent_port,
                mtconnect_version=str(mtc_version),
                asset_buffer_size=int(asset_buf),
                buffer_size=int(buf_size),
                player_host=str(player_host),
                player_port=player_port,
            )
            # The equipment type's dedicated create model — the axis-limit
            # fields are typed per axis (LinearAxisLimits / RotaryAxisLimits).
            limit_kwargs = {
                name: type(create_model.model_fields[name].default)(min=lo, max=hi)
                for name, (lo, hi) in limit_values.items()
            }
            try:
                client.simulate_start(create_model(
                    device_name=final_config.device_name,
                    routine=final_config.routine,
                    interval_ms=final_config.interval_ms,
                    agent=AgentSettings(
                        port=final_config.agent_port,
                        mtconnect_version=final_config.mtconnect_version,
                        asset_buffer_size=final_config.asset_buffer_size,
                        buffer_size=final_config.buffer_size,
                    ),
                    shdr=ShdrSettings(
                        host=final_config.player_host,
                        port=final_config.player_port,
                    ),
                    **limit_kwargs,
                ))
            except ApiError as ex:
                if ex.code == "port_conflict":
                    st.warning("Those ports were just taken by another session. Adjust and retry.")
                else:
                    feed.post_ui(f"start simulation failed: {ex.message}")
                    st.error(f"Failed to start simulation: {ex.message}")
            else:
                # Remember non-port settings as defaults for the next launch.
                with get_state_lock():
                    state.config = final_config
                    state.gen += 1
                st.rerun()

    # --- Log Output (full width) ---
    st.subheader("Log Output")
    # Fragments (partial reruns) keep the log streaming and the grid in sync
    # with API-driven changes; a full-page rerun only fires when this page's
    # session table actually changed, so an open 3D-view dialog is never
    # dismissed by a quiet backend.
    _log_output_fragment()

    _watch_backend_fragment()
