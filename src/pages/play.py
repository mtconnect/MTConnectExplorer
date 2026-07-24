from typing import List

import pandas as pd
import streamlit as st

from mtconnect_explorer_client import ApiError
from mtconnect_explorer_client.schemas import (
    AgentSettings,
    PlayCreate,
    PlayDevice,
)

from src.pages.api import get_client, get_feed
from src.pages.core import (
    PlayConfig,
    agent_url,
    get_play_state,
    get_state_lock,
    log_area,
)


def _next_free_port(start: int, used: set) -> int:
    p = start
    while p in used:
        p += 1
    return p


def _backend_fingerprint() -> tuple:
    """What this page renders from the backend: play sessions (id, status),
    the dataset names + in-use statuses (drives the file selector), and the
    used-port set (drives the port suggestions/validation)."""
    client = get_client()
    return (
        tuple(sorted((s.id, s.status) for s in client.play_list())),
        tuple(sorted((d.name, tuple(d.in_use)) for d in client.datasets_list())),
        tuple(sorted(client.ports().used)),
    )


@st.fragment(run_every=1.0)
def _watch_backend_fragment() -> None:
    """The API can change sessions/datasets behind this page's back — poll the
    backend and trigger a full-page rerun only when what the page rendered has
    actually changed, so idle pages pick up API-driven starts/stops without
    constant rerun churn."""
    try:
        current = _backend_fingerprint()
    except ApiError:
        return
    if current != st.session_state.get("play_backend_fp"):
        st.rerun(scope="app")


@st.fragment(run_every=0.5)
def _log_output_fragment() -> None:
    """Live-refresh the consolidated log via a fragment (partial) rerun."""
    log_area("play_log_area", get_feed("play").lines)


def play_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Play")

    client = get_client()
    feed = get_feed("play")
    state = get_play_state()

    # Auto-delete terminal sessions (finished/error) so the grid reaps
    # instantly, exactly like the pre-API behavior (e.g. loop-off completion).
    sessions = []
    try:
        for sess in client.play_list():
            if sess.status in ("finished", "error"):
                try:
                    client.play_stop(sess.id)
                except ApiError:
                    pass
            else:
                sessions.append(sess)
    except ApiError as ex:
        st.error(f"API error: {ex.message}")
    feed.update_labels(sessions)
    # Snapshot what this render is based on; the watcher fragment reruns the
    # page when the backend diverges from it (e.g. API-driven start/stop).
    try:
        st.session_state["play_backend_fp"] = _backend_fingerprint()
    except ApiError:
        pass

    play_config: PlayConfig = state.config

    # Pre-create both sections as stable top-level containers (always, in the
    # same order) so the form's columns are never reconciled against the
    # conditionally-rendered Now Playing columns — that mismatch caused the
    # form to ghost/duplicate beside Now Playing right after launching.
    now_playing_container = st.container()
    form_container = st.container()

    # --- Now Playing ---
    if sessions:
        with now_playing_container:
            head_l, head_r = st.columns([3, 1])
            head_l.subheader("Now Playing")
            with head_r:
                if st.button("Stop All", width="stretch"):
                    try:
                        client.play_stop_all()
                    except ApiError as ex:
                        feed.post_ui(f"stop all failed: {ex.message}")
                    st.rerun()

            h = st.columns([3, 2, 3, 2, 1])
            h[0].markdown("**Dataset**")
            h[1].markdown("**Device**")
            h[2].markdown("**Agent URL**")
            h[3].markdown("**Status**")
            h[4].markdown("**Stop**")
            for sess in sessions:
                c = st.columns([3, 2, 3, 2, 1])
                c[0].write(sess.config.dataset)
                c[1].write(", ".join(d.device_name for d in sess.config.devices))
                c[2].write(agent_url(sess.config.agent.port))
                if sess.status == "running":
                    status = "looping" if sess.config.loop else "playing"
                elif sess.status == "starting":
                    # Agent still booting / connecting to the SHDR adapter(s).
                    status = "starting"
                else:
                    status = "stopping"
                c[3].write(status)
                with c[4]:
                    if st.button("Stop", key=f"play_stop_{sess.id}", width="stretch"):
                        try:
                            client.play_stop(sess.id)
                        except ApiError as ex:
                            feed.post_ui(f"stop failed: {ex.message}")
                        st.rerun()
            st.divider()

    # --- Start Playback (kept narrow; it looks best at this width) ---
    _, form_col, _ = form_container.columns([1, 2, 1])
    with form_col:
        st.subheader("Start Playback")
        # Exclude datasets that are currently being written by a capture session.
        try:
            file_names = [
                d.name for d in client.datasets_list() if "capture" not in d.in_use
            ]
        except ApiError as ex:
            st.error(f"API error: {ex.message}")
            file_names = []
        if not file_names:
            st.warning("No datasets found. Use the Capture page to record one first.")

        dataset_file = st.selectbox(
            "Dataset File",
            file_names if file_names else ["(none)"],
            disabled=not file_names,
            key="play_dataset",
            filter_mode=None,  # Disable search/text input
        )

        # Load device names from selected dataset (filtering out the Agent
        # device is a frontend concern — the endpoint returns all devices).
        device_names: List[str] = []
        if file_names and dataset_file and dataset_file != "(none)":
            try:
                device_names = sorted(
                    d.name
                    for d in client.datasets_devices(str(dataset_file)).devices
                    if d.name != "Agent"
                )
            except ApiError:
                device_names = []

        re_timestamp = st.checkbox(
            "Re-Timestamp",
            value=play_config.re_timestamp,
            key="play_re_timestamp",
            help="Replace recorded timestamps with the current time during playback so "
            "the data appears live. When disabled, the original recorded timestamps "
            "are preserved.",
        )

        loop = st.checkbox(
            "Loop",
            value=play_config.loop,
            key="play_loop",
            help="Replay the dataset repeatedly. After the first pass, the device "
            "model is reused and only the recorded observations are replayed.",
        )

        # Ports must be unique across simultaneously-running Play/Simulator
        # sessions. Pre-fill free ports (from the backend's suggestions) but let
        # the user override; widget keys embed a generation counter so they
        # re-seed to the next free ports after each launch while still
        # persisting manual edits between launches.
        try:
            ports_info = client.ports(shdr_count=len(device_names))
            used_ports = set(ports_info.used)
            suggested_agent = ports_info.suggested.agent
            suggested_shdr = ports_info.suggested.shdr
        except ApiError:
            used_ports = set()
            suggested_agent = PlayConfig.model_fields["agent_port"].default
            suggested_shdr = []
        gen = state.gen

        # --- Devices: one selectable SHDR adapter per device in the dataset ---
        st.subheader("Devices")
        default_host = PlayDevice.model_fields["shdr_host"].default
        seed_rows = []
        for i, name in enumerate(device_names):
            port = (
                suggested_shdr[i]
                if i < len(suggested_shdr)
                else _next_free_port(
                    PlayDevice.model_fields["shdr_port"].default,
                    used_ports | set(suggested_shdr),
                )
            )
            seed_rows.append({
                "Enabled": True,
                "Device": name,
                "SHDR Adapter Host": default_host,
                "SHDR Adapter Port": port,
            })

        devices_df = None
        if device_names:
            devices_df = st.data_editor(
                pd.DataFrame(
                    seed_rows,
                    columns=["Enabled", "Device", "SHDR Adapter Host", "SHDR Adapter Port"],
                ),
                column_config={
                    "Enabled": st.column_config.CheckboxColumn("Enabled", width="small"),
                    "Device": st.column_config.TextColumn("Device", disabled=True),
                    "SHDR Adapter Host": st.column_config.TextColumn("SHDR Adapter Host"),
                    "SHDR Adapter Port": st.column_config.NumberColumn(
                        "SHDR Adapter Port", min_value=1, max_value=65535, step=1, format="%d",
                    ),
                },
                hide_index=True,
                num_rows="fixed",
                width="stretch",
                key=f"play_devices_{dataset_file}_{gen}",
            )

        # Enabled rows -> the adapters this session will spin up.
        selected: List[PlayDevice] = []
        if devices_df is not None:
            for _, row in devices_df.iterrows():
                if bool(row["Enabled"]):
                    selected.append(PlayDevice(
                        device_name=str(row["Device"]),
                        shdr_host=str(row["SHDR Adapter Host"]).strip(),
                        shdr_port=int(row["SHDR Adapter Port"]),
                    ))

        # --- Agent config ---
        st.subheader("Agent Configuration")
        c1, c2 = st.columns(2)
        with c1:
            agent_port = st.number_input(
                "Port", value=suggested_agent, min_value=1, max_value=65535, step=1,
                key=f"play_agent_port_{gen}",
            )
            mtc_version = st.text_input(
                "MTConnect Version", value=play_config.mtconnect_version,
                key="play_mtc_version",
            )
        with c2:
            asset_buf = st.number_input(
                "Asset Buffer Size", value=play_config.asset_buffer_size, min_value=1, step=1,
                key="play_asset_buf",
            )
            buf_size = st.number_input(
                "Buffer Size", value=play_config.buffer_size, min_value=1, step=1,
                key="play_buf_size",
            )

        # Validate ports client-side for instant feedback; the backend
        # re-validates atomically on POST and is the real gate.
        agent_port = int(agent_port)
        conflicts: List[str] = []
        if not selected:
            conflicts.append("enable at least one device")
        if agent_port in used_ports:
            conflicts.append(f"agent port {agent_port} is in use")
        seen_ports: dict = {}
        for d in selected:
            if d.shdr_port == agent_port:
                conflicts.append(f"{d.device_name}: SHDR port {d.shdr_port} equals the agent port")
            if d.shdr_port in used_ports:
                conflicts.append(f"{d.device_name}: SHDR port {d.shdr_port} is in use")
            seen_ports.setdefault(d.shdr_port, []).append(d.device_name)
        for port, names in seen_ports.items():
            if len(names) > 1:
                conflicts.append(f"SHDR port {port} reused by {', '.join(names)}")
        ports_valid = not conflicts
        if conflicts:
            st.error("Cannot start: " + "; ".join(conflicts) + ".")

        # --- Play ---
        play_clicked = st.button(
            "Play",
            disabled=not file_names or not device_names or not ports_valid,
            width="stretch", type="primary",
        )

        if play_clicked and file_names and selected:
            try:
                client.play_start(PlayCreate(
                    dataset=str(dataset_file),
                    devices=selected,
                    agent=AgentSettings(
                        port=agent_port,
                        mtconnect_version=str(mtc_version),
                        asset_buffer_size=int(asset_buf),
                        buffer_size=int(buf_size),
                    ),
                    re_timestamp=bool(re_timestamp),
                    loop=bool(loop),
                ))
            except ApiError as ex:
                if ex.code == "port_conflict":
                    st.warning("Those ports were just taken by another session. Adjust and retry.")
                else:
                    feed.post_ui(f"start playback failed: {ex.message}")
                    st.error(f"Failed to start playback: {ex.message}")
            else:
                # Remember non-device settings as defaults for the next launch.
                with get_state_lock():
                    state.config = PlayConfig(
                        agent_port=agent_port,
                        mtconnect_version=str(mtc_version),
                        asset_buffer_size=int(asset_buf),
                        buffer_size=int(buf_size),
                        re_timestamp=bool(re_timestamp),
                        loop=bool(loop),
                    )
                    state.gen += 1
                st.rerun()

    # --- Log Output (full width) ---
    st.subheader("Log Output")
    _log_output_fragment()

    # Keep the page in sync with API-driven changes (change-triggered rerun —
    # a quiet backend never disturbs the form or an in-progress device edit).
    _watch_backend_fragment()
