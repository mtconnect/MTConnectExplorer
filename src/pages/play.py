import logging
import queue
import threading
import time
from typing import List, Optional

import streamlit as st
from pydantic import BaseModel, Field

from src.agent import Agent, AgentConfig, AgentAdapter
from src.converter import MtConnectDatasetPlayer, XmlToShdrConverter
from src.pages.core import (
    DATASETS_DIR,
    attach_log,
    detach_log,
    drain,
    log_area,
)


class PlayConfig(BaseModel):
    agent_port: int = Field(
        AgentConfig.model_fields["port"].default,
        description="MTConnect agent HTTP port",
    )
    mtconnect_version: str = Field(
        AgentConfig.model_fields["mtconnect_version"].default,
        description="MTConnect version string",
    )
    asset_buffer_size: int = Field(
        AgentConfig.model_fields["asset_buffer_size"].default,
        description="Agent asset buffer size",
    )
    buffer_size: int = Field(
        AgentConfig.model_fields["buffer_size"].default,
        description="Agent circular buffer size",
    )
    player_host: str = Field(
        AgentAdapter.model_fields["host"].default,
        description="SHDR adapter host address",
    )
    player_port: int = Field(
        AgentAdapter.model_fields["port"].default,
        description="SHDR adapter port",
    )
    device_name: str = Field(
        "",
        description="Device name from dataset to play back",
    )


def _play_worker(
    play_config: PlayConfig,
    dataset_path: str,
    stop_event: threading.Event,
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

        # while not stop_event.is_set():
        #     time.sleep(0.1)

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


def _stop_play() -> None:
    if st.session_state.play_stop_event:
        st.session_state.play_stop_event.set()
    worker: Optional[threading.Thread] = st.session_state.play_worker
    if worker and worker.is_alive():
        worker.join(timeout=15)
    detach_log(st.session_state.play_handler)
    drain(st.session_state.play_log_q, st.session_state.play_log_lines)
    st.session_state.play_running = False
    st.session_state.play_handler = None
    st.session_state.play_worker = None
    st.session_state.play_stop_event = None


def play_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="centered")
    st.title("Play")

    if "play_config" not in st.session_state:
        st.session_state.play_config = PlayConfig()
    if "play_running" not in st.session_state:
        st.session_state.play_running = False
    if "play_log_q" not in st.session_state:
        st.session_state.play_log_q = queue.Queue()
    if "play_log_lines" not in st.session_state:
        st.session_state.play_log_lines = []
    if "play_handler" not in st.session_state:
        st.session_state.play_handler = None
    if "play_worker" not in st.session_state:
        st.session_state.play_worker = None
    if "play_stop_event" not in st.session_state:
        st.session_state.play_stop_event = None

    drain(st.session_state.play_log_q, st.session_state.play_log_lines)

    # Auto-stop when worker thread finishes
    if st.session_state.play_running and st.session_state.play_worker:
        if not st.session_state.play_worker.is_alive():
            _stop_play()
            st.rerun()

    disabled = st.session_state.play_running
    play_config: PlayConfig = st.session_state.play_config

    # --- Dataset ---
    st.subheader("Dataset")
    DATASETS_DIR.mkdir(exist_ok=True)
    files = sorted(DATASETS_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    file_names = [f.name for f in files]
    if not file_names:
        st.warning("No datasets found. Use the Capture page to record one first.")

    dataset_file = st.selectbox(
        "Dataset File",
        file_names if file_names else ["(none)"],
        disabled=disabled or not file_names,
        key="play_dataset",
        filter_mode=None, # Disable search/text input
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
        disabled=disabled or not device_names,
        key="play_device_name",
        filter_mode=None, # Disable search/text input
    )

    # --- Agent config ---
    st.subheader("Agent Configuration")
    c1, c2 = st.columns(2)
    with c1:
        agent_port = st.number_input(
            "Port", value=play_config.agent_port, min_value=1, max_value=65535, step=1,
            key="play_agent_port", disabled=disabled,
        )
        mtc_version = st.text_input(
            "MTConnect Version", value=play_config.mtconnect_version,
            key="play_mtc_version", disabled=disabled,
        )
    with c2:
        asset_buf = st.number_input(
            "Asset Buffer Size", value=play_config.asset_buffer_size, min_value=1, step=1,
            key="play_asset_buf", disabled=disabled,
        )
        buf_size = st.number_input(
            "Buffer Size", value=play_config.buffer_size, min_value=1, step=1,
            key="play_buf_size", disabled=disabled,
        )

    # --- SHDR Adapter ---
    st.subheader("SHDR Adapter")
    c1, c2 = st.columns(2)
    with c1:
        player_host = st.text_input(
            "Host", value=play_config.player_host, key="play_player_host", disabled=disabled,
        )
    with c2:
        player_port = st.number_input(
            "Port", value=play_config.player_port, min_value=1, max_value=65535, step=1,
            key="play_player_port", disabled=disabled,
        )

    # --- Play / Stop ---
    c1, c2 = st.columns(2)
    with c1:
        play_clicked = st.button(
            "Play", disabled=disabled or not file_names or not device_names,
            use_container_width=True, type="primary",
        )
    with c2:
        stop_clicked = st.button("Stop", disabled=not disabled, use_container_width=True)

    if play_clicked and not st.session_state.play_running and file_names and device_names:
        final_config = PlayConfig(
            agent_port=int(agent_port),
            mtconnect_version=str(mtc_version),
            asset_buffer_size=int(asset_buf),
            buffer_size=int(buf_size),
            player_host=str(player_host),
            player_port=int(player_port),
            device_name=str(device_name),
        )
        st.session_state.play_config = final_config

        q: queue.Queue = queue.Queue()
        stop_event = threading.Event()
        handler = attach_log(q)
        worker = threading.Thread(
            target=_play_worker,
            args=(final_config, str(DATASETS_DIR / dataset_file), stop_event),
            name="streamlit-play-worker",
            daemon=True,
        )
        st.session_state.play_log_q = q
        st.session_state.play_log_lines = []
        st.session_state.play_handler = handler
        st.session_state.play_stop_event = stop_event
        st.session_state.play_worker = worker
        st.session_state.play_running = True
        worker.start()
        st.rerun()

    if stop_clicked and st.session_state.play_running:
        _stop_play()
        st.rerun()

    st.subheader("Log Output")
    log_area("play_log_area", st.session_state.play_log_lines)

    if st.session_state.play_running:
        time.sleep(0.5)
        st.rerun()
