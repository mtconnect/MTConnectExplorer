import logging
import threading
import time
from typing import List, Optional

import pandas as pd
import streamlit as st

from src.agent import Agent, AgentConfig, AgentAdapter
from src.converter import MtConnectDatasetPlayer, XmlToShdrConverter
from src.pages.core import (
    DATASETS_DIR,
    PlayConfig,
    PlayDeviceAdapter,
    PlaybackSession,
    PlayPageState,
    agent_url,
    all_used_ports,
    attach_session_log,
    detach_log,
    drain,
    get_capture_state,
    get_play_state,
    get_state_lock,
    log_area,
    session_thread_name,
)


def _play_worker(
    play_config: PlayConfig,
    dataset_path: str,
    stop_event: threading.Event,
    started_event: threading.Event,
    log_label: str,
) -> None:
    _log = logging.getLogger(__name__)
    agent: Optional[Agent] = None
    players: List[MtConnectDatasetPlayer] = []
    try:
        _log.info("starting agent...")

        # One agent fronting one SHDR adapter per selected device.
        agent = Agent(AgentConfig(
            adapters=[
                AgentAdapter(name=d.device_name, host=d.player_host, port=d.player_port)
                for d in play_config.devices
            ],
            port=play_config.agent_port,
            mtconnect_version=play_config.mtconnect_version,
            asset_buffer_size=play_config.asset_buffer_size,
            buffer_size=play_config.buffer_size,
        )).start()

        _log.info("starting %d adapter(s)...", len(play_config.devices))

        # Each player binds its own SHDR adapter (autoplay deferred) and reads the
        # same dataset file through its own handle, filtered to its device.
        for d in play_config.devices:
            players.append(MtConnectDatasetPlayer(
                d.player_host,
                d.player_port,
                play_config.re_timestamp,
                play_config.loop,
                thread_name=session_thread_name(log_label, f"dataset-player-{d.device_name}"),
            ).open(dataset_path, d.device_name, False))

        _log.info("waiting for agent to connect to all adapters...")
        while not all(p._adapter.agent_is_connected for p in players):
            if stop_event.is_set():
                _log.info("playback aborted")
                return
            time.sleep(0.1)
        for p in players:
            p.play()
        # The agent has connected to every adapter and data is now streaming;
        # flip the session status from "starting" to playing.
        started_event.set()

        _log.info("playback started")

        while not all(p.is_finished for p in players):
            if stop_event.is_set():
                break
            time.sleep(0.1)

        if not stop_event.is_set():
            _log.info("playback complete")
        else:
            _log.info("playback stopped")

    except Exception as ex:
        _log.error(f"play worker error: {ex}")
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


def _session_label(dataset_file: str, device_names: List[str], agent_port: int) -> str:
    """Human-readable session label used to tag log lines and label the grid."""
    devices = ", ".join(device_names) if device_names else "no devices"
    return f"{dataset_file} | {devices} | localhost:{agent_port}"


def _next_free_port(start: int, used: set) -> int:
    p = start
    while p in used:
        p += 1
    return p


def _used_ports(state: PlayPageState) -> set:
    # Ports are de-conflicted across BOTH Play and Simulator sessions so a
    # playback and a simulation can never collide on the same agent/SHDR port.
    return all_used_ports()


def _sync_log_handler(state: PlayPageState) -> None:
    """Detach the consolidated log handler once no sessions remain. Caller holds the lock."""
    if not state.sessions and state.handler is not None:
        detach_log(state.handler)
        state.handler = None


def _remove_session(state: PlayPageState, sid: str) -> None:
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


def _reap_finished(state: PlayPageState) -> None:
    """Drop sessions whose worker exited on its own (e.g. loop disabled, completed).
    Caller holds the lock."""
    for sid, sess in list(state.sessions.items()):
        worker = sess.worker
        if worker is None or not worker.is_alive():
            if worker is not None:
                worker.join(timeout=1)
            state.sessions.pop(sid, None)
    _sync_log_handler(state)


def play_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Play")

    state = get_play_state()

    drain(state.log_q, state.log_lines)

    # Reap sessions whose worker has finished/died so the grid stays accurate.
    with get_state_lock():
        _reap_finished(state)

    play_config: PlayConfig = state.config

    # Pre-create both sections as stable top-level containers (always, in the
    # same order) so the form's columns are never reconciled against the
    # conditionally-rendered Now Playing columns — that mismatch caused the
    # form to ghost/duplicate beside Now Playing right after launching.
    now_playing_container = st.container()
    form_container = st.container()

    # --- Now Playing ---
    if state.sessions:
        with now_playing_container:
            head_l, head_r = st.columns([3, 1])
            head_l.subheader("Now Playing")
            with head_r:
                if st.button("Stop All", width="stretch"):
                    with get_state_lock():
                        for sid in list(state.sessions):
                            _remove_session(state, sid)
                    st.rerun()

            h = st.columns([3, 2, 3, 2, 1])
            h[0].markdown("**Dataset**")
            h[1].markdown("**Device**")
            h[2].markdown("**Agent URL**")
            h[3].markdown("**Status**")
            h[4].markdown("**Stop**")
            for sid, sess in list(state.sessions.items()):
                c = st.columns([3, 2, 3, 2, 1])
                c[0].write(sess.dataset_file)
                c[1].write(", ".join(d.device_name for d in sess.config.devices))
                c[2].write(agent_url(sess.config.agent_port))
                alive = sess.worker is not None and sess.worker.is_alive()
                started_ev = getattr(sess, "started", None)
                started = started_ev is not None and started_ev.is_set()
                if not alive:
                    status = "stopping"
                elif not started:
                    # Agent still booting / connecting to the SHDR adapter(s).
                    status = "starting"
                else:
                    status = "looping" if sess.config.loop else "playing"
                c[3].write(status)
                with c[4]:
                    if st.button("Stop", key=f"play_stop_{sid}", width="stretch"):
                        with get_state_lock():
                            _remove_session(state, sid)
                        st.rerun()
            st.divider()

    # --- Start Playback (kept narrow; it looks best at this width) ---
    _, form_col, _ = form_container.columns([1, 2, 1])
    with form_col:
        st.subheader("Start Playback")
        DATASETS_DIR.mkdir(exist_ok=True)
        # Exclude datasets that are currently being written by a capture session.
        capturing = {s.filename for s in get_capture_state().sessions.values()}
        files = sorted(DATASETS_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        file_names = [f.name for f in files if f.name not in capturing]
        if not file_names:
            st.warning("No datasets found. Use the Capture page to record one first.")

        dataset_file = st.selectbox(
            "Dataset File",
            file_names if file_names else ["(none)"],
            disabled=not file_names,
            key="play_dataset",
            filter_mode=None,  # Disable search/text input
        )

        # Load device names from selected dataset
        device_names: List[str] = []
        if file_names and dataset_file and dataset_file != "(none)":
            try:
                device_names = sorted(
                    n for n in XmlToShdrConverter.get_device_names(str(DATASETS_DIR / dataset_file))
                    if n != "Agent"
                )
            except Exception:
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
        # sessions. Pre-fill free ports but let the user override; widget keys embed
        # the session counter so they re-seed to the next free ports after each
        # launch while still persisting manual edits between launches.
        used_ports = _used_ports(state)
        suggested_agent = _next_free_port(PlayConfig.model_fields["agent_port"].default, used_ports)
        gen = state.next_id

        # --- Devices: one selectable SHDR adapter per device in the dataset ---
        st.subheader("Devices")
        default_host = AgentAdapter.model_fields["host"].default
        # Seed each device a distinct free SHDR port (skipping ports already in use
        # and those just assigned to earlier rows).
        seeded = set(used_ports)
        seed_rows = []
        next_port = AgentAdapter.model_fields["port"].default
        for name in device_names:
            next_port = _next_free_port(next_port, seeded)
            seeded.add(next_port)
            seed_rows.append({
                "Enabled": True,
                "Device": name,
                "SHDR Adapter Host": default_host,
                "SHDR Adapter Port": next_port,
            })
            next_port += 1

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
        selected: List[PlayDeviceAdapter] = []
        if devices_df is not None:
            for _, row in devices_df.iterrows():
                if bool(row["Enabled"]):
                    selected.append(PlayDeviceAdapter(
                        device_name=str(row["Device"]),
                        player_host=str(row["SHDR Adapter Host"]).strip(),
                        player_port=int(row["SHDR Adapter Port"]),
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

        # Validate ports; disable Play until valid.
        agent_port = int(agent_port)
        conflicts: List[str] = []
        if not selected:
            conflicts.append("enable at least one device")
        if agent_port in used_ports:
            conflicts.append(f"agent port {agent_port} is in use")
        seen_ports: dict = {}
        for d in selected:
            if d.player_port == agent_port:
                conflicts.append(f"{d.device_name}: SHDR port {d.player_port} equals the agent port")
            if d.player_port in used_ports:
                conflicts.append(f"{d.device_name}: SHDR port {d.player_port} is in use")
            seen_ports.setdefault(d.player_port, []).append(d.device_name)
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
            with get_state_lock():
                # Re-validate under the lock — another user may have taken a port.
                used_now = _used_ports(state)
                clashing = [d.device_name for d in selected if d.player_port in used_now]
                if agent_port in used_now or clashing:
                    st.warning("Those ports were just taken by another session. Adjust and retry.")
                else:
                    final_config = PlayConfig(
                        agent_port=agent_port,
                        mtconnect_version=str(mtc_version),
                        asset_buffer_size=int(asset_buf),
                        buffer_size=int(buf_size),
                        re_timestamp=bool(re_timestamp),
                        loop=bool(loop),
                        devices=selected,
                    )
                    # Remember non-device settings as defaults for the next launch.
                    state.config = final_config

                    sid = str(state.next_id)
                    state.next_id += 1
                    device_names_sel = [d.device_name for d in selected]
                    label = _session_label(str(dataset_file), device_names_sel, agent_port)

                    if state.handler is None:
                        state.handler = attach_session_log(state.log_q, state.sessions)

                    stop_event = threading.Event()
                    started_event = threading.Event()
                    worker = threading.Thread(
                        target=_play_worker,
                        args=(final_config, str(DATASETS_DIR / dataset_file),
                              stop_event, started_event, label),
                        name=session_thread_name(label, "worker"),
                        daemon=True,
                    )
                    state.sessions[sid] = PlaybackSession(
                        id=sid,
                        label=label,
                        dataset_file=str(dataset_file),
                        config=final_config,
                        worker=worker,
                        stop_event=stop_event,
                        started=started_event,
                    )
                    worker.start()
                    st.rerun()

    # --- Log Output (full width) ---
    st.subheader("Log Output")
    log_area("play_log_area", state.log_lines)

    if state.sessions:
        time.sleep(0.5)
        st.rerun()
