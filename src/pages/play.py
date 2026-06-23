import logging
import threading
import time
from typing import List, Optional

import streamlit as st

from src.agent import Agent, AgentConfig, AgentAdapter
from src.converter import MtConnectDatasetPlayer, XmlToShdrConverter
from src.pages.core import (
    DATASETS_DIR,
    PlayConfig,
    PlaybackSession,
    PlayPageState,
    agent_url,
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
    log_label: str,
) -> None:
    _log = logging.getLogger(__name__)
    agent: Optional[Agent] = None
    player: Optional[MtConnectDatasetPlayer] = None
    try:
        _log.info("starting agent...")

        agent = Agent(AgentConfig(
            adapters=[AgentAdapter(
                name=play_config.device_name,
                host=play_config.player_host,
                port=play_config.player_port,
            )],
            port=play_config.agent_port,
            mtconnect_version=play_config.mtconnect_version,
            asset_buffer_size=play_config.asset_buffer_size,
            buffer_size=play_config.buffer_size,
        )).start()

        _log.info("starting adapter...")

        player = MtConnectDatasetPlayer(
            play_config.player_host,
            play_config.player_port,
            play_config.re_timestamp,
            play_config.loop,
            thread_name=session_thread_name(log_label, "dataset-player"),
        ).open(dataset_path, play_config.device_name, False)

        _log.info("waiting for agent to connect to adapter...")
        while not player._adapter.agent_is_connected:
            if stop_event.is_set():
                _log.info("playback aborted")
                return
            time.sleep(0.1)
        player.play()

        _log.info("playback started")

        while not player.is_finished:
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
        try:
            if player is not None:
                player.stop()
        except Exception:
            pass
        try:
            if agent is not None:
                agent.stop()
        except Exception:
            pass


def _session_label(dataset_file: str, device_name: str, agent_port: int) -> str:
    """Human-readable session label used to tag log lines and label the grid."""
    return f"{dataset_file} | {device_name} | localhost:{agent_port}"


def _next_free_port(start: int, used: set) -> int:
    p = start
    while p in used:
        p += 1
    return p


def _used_ports(state: PlayPageState) -> set:
    used: set = set()
    for s in state.sessions.values():
        used.add(s.config.agent_port)
        used.add(s.config.player_port)
    return used


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
                if st.button("Stop All", use_container_width=True):
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
                c[1].write(sess.config.device_name)
                c[2].write(agent_url(sess.config.agent_port))
                alive = sess.worker is not None and sess.worker.is_alive()
                c[3].write(("looping" if sess.config.loop else "playing") if alive else "stopping")
                with c[4]:
                    if st.button("Stop", key=f"play_stop_{sid}", use_container_width=True):
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

        current_device_name = play_config.device_name
        if current_device_name not in device_names:
            current_device_name = device_names[0] if device_names else ""

        device_name = st.selectbox(
            "Device",
            device_names if device_names else ["(none)"],
            index=device_names.index(current_device_name) if current_device_name in device_names else 0,
            disabled=not device_names,
            key="play_device_name",
            filter_mode=None,  # Disable search/text input
        )

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

        # Ports must be unique across simultaneously-running sessions. Pre-fill the
        # next free ports, but let the user override. The widget keys include the
        # session counter so they re-seed to the next free port after each launch
        # while still persisting manual edits between launches.
        used_ports = _used_ports(state)
        suggested_agent = _next_free_port(PlayConfig.model_fields["agent_port"].default, used_ports)
        suggested_player = _next_free_port(PlayConfig.model_fields["player_port"].default, used_ports)
        gen = state.next_id

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

        # --- SHDR Adapter ---
        st.subheader("SHDR Adapter")
        c1, c2 = st.columns(2)
        with c1:
            player_host = st.text_input(
                "Host", value=play_config.player_host, key="play_player_host",
            )
        with c2:
            player_port = st.number_input(
                "Port", value=suggested_player, min_value=1, max_value=65535, step=1,
                key=f"play_player_port_{gen}",
            )

        # Validate ports against running sessions; disable Play until they are valid.
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

        # --- Play ---
        play_clicked = st.button(
            "Play",
            disabled=not file_names or not device_names or not ports_valid,
            use_container_width=True, type="primary",
        )

        if play_clicked and file_names and device_names:
            with get_state_lock():
                # Re-validate under the lock — another user may have taken the port.
                used_now = _used_ports(state)
                if agent_port in used_now or player_port in used_now or agent_port == player_port:
                    st.warning("Those ports were just taken by another session. Adjust and retry.")
                else:
                    final_config = PlayConfig(
                        agent_port=agent_port,
                        mtconnect_version=str(mtc_version),
                        asset_buffer_size=int(asset_buf),
                        buffer_size=int(buf_size),
                        player_host=str(player_host),
                        player_port=player_port,
                        device_name=str(device_name),
                        re_timestamp=bool(re_timestamp),
                        loop=bool(loop),
                    )
                    # Remember non-port settings as defaults for the next launch.
                    state.config = final_config

                    sid = str(state.next_id)
                    state.next_id += 1
                    label = _session_label(str(dataset_file), str(device_name), agent_port)

                    if state.handler is None:
                        state.handler = attach_session_log(state.log_q, state.sessions)

                    stop_event = threading.Event()
                    worker = threading.Thread(
                        target=_play_worker,
                        args=(final_config, str(DATASETS_DIR / dataset_file), stop_event, label),
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
                    )
                    worker.start()
                    st.rerun()

    # --- Log Output (full width) ---
    st.subheader("Log Output")
    log_area("play_log_area", state.log_lines)

    if state.sessions:
        time.sleep(0.5)
        st.rerun()
