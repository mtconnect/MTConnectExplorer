import logging
import queue
import re
import time
from datetime import datetime, timezone

import streamlit as st
from pydantic import BaseModel, Field

from src.client import (
    MtConnectClientConfiguration,
    MtConnectDataCollector,
    MtConnectDataCollectorConfiguration,
)
from src.pages.core import (
    DATASETS_DIR,
    attach_log,
    detach_log,
    drain,
    log_area,
)


_log = logging.getLogger(__name__)


class CaptureConfig(BaseModel):
    base_url: str = Field(
        "https://demo.mtconnect.org/", 
        description="Base URL of the MTConnect agent",
    )
    timeout: float = Field(
        MtConnectClientConfiguration.model_fields["timeout"].default,
        description="Socket timeout in seconds for non-streaming requests",
    )
    chunk_size: int = Field(
        MtConnectClientConfiguration.model_fields["chunk_size"].default,
        description="Read chunk size in bytes for streaming responses",
    )
    interval_ms: int = Field(
        MtConnectDataCollectorConfiguration.model_fields["stream_interval_ms"].default,
        description="Milliseconds between agent stream buffer checks",
    )
    mode: str = Field(
        MtConnectDataCollectorConfiguration.model_fields["mode"].default,
        description="Data collection mode: stream, sample, or current",
    )


def _filename(base_url: str) -> str:
    cleaned = re.sub(
        r"_+", "_",
        re.sub(r"[^a-zA-Z0-9._-]", "_", base_url.replace("://", "_").rstrip("/"))
    ).strip("_")
    return f"{cleaned}-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}.jsonl"


def capture_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="centered")
    st.title("Capture")

    if "cap_config" not in st.session_state:
        st.session_state.cap_config = CaptureConfig()
    if "cap_running" not in st.session_state:
        st.session_state.cap_running = False
    if "cap_log_q" not in st.session_state:
        st.session_state.cap_log_q = queue.Queue()
    if "cap_log_lines" not in st.session_state:
        st.session_state.cap_log_lines = []
    if "cap_collector" not in st.session_state:
        st.session_state.cap_collector = None
    if "cap_handler" not in st.session_state:
        st.session_state.cap_handler = None
    if "cap_filename" not in st.session_state:
        st.session_state.cap_filename = ""

    drain(st.session_state.cap_log_q, st.session_state.cap_log_lines)

    disabled = st.session_state.cap_running
    config: CaptureConfig = st.session_state.cap_config

    st.subheader("Configuration")
    base_url = st.text_input("Base URL", value=config.base_url, key="cap_base_url", disabled=disabled)
    c1, c2 = st.columns(2)
    with c1:
        # timeout = st.number_input(
        #     "Timeout (s)", value=config.timeout, min_value=1.0,
        #     key="cap_timeout", disabled=disabled,
        # )
        stream_interval_ms = st.number_input(
            "Interval (ms)", value=config.interval_ms, min_value=1, step=1,
            key="cap_stream_interval_ms", disabled=disabled,
        )
    with c2:
        # chunk_size = st.number_input(
        #     "Chunk Size", value=config.chunk_size, min_value=1, step=1,
        #     key="cap_chunk", disabled=disabled,
        # )
        _modes = ["stream", "sample", "current"]
        mode = st.selectbox(
            "Mode", _modes,
            index=_modes.index(config.mode) if config.mode in _modes else 0,
            key="cap_mode", disabled=disabled,
            filter_mode=None, # Disable search/text input
        )

    c1, c2 = st.columns(2)
    with c1:
        start_clicked = st.button("Start", disabled=disabled, use_container_width=True, type="primary")
    with c2:
        stop_clicked = st.button("Stop", disabled=not disabled, use_container_width=True)

    if start_clicked and not st.session_state.cap_running:
        new_config = CaptureConfig(
            base_url=str(base_url),
            #timeout=float(timeout),
            #chunk_size=int(chunk_size),
            interval_ms=int(stream_interval_ms),
            mode=str(mode),
        )
        st.session_state.cap_config = new_config
        DATASETS_DIR.mkdir(exist_ok=True)
        filename = _filename(new_config.base_url)
        q: queue.Queue = queue.Queue()
        handler = attach_log(q)
        collector = MtConnectDataCollector(
            MtConnectDataCollectorConfiguration(
                configuration=MtConnectClientConfiguration(
                    base_url=new_config.base_url,
                    timeout=new_config.timeout,
                    chunk_size=new_config.chunk_size,
                ),
                stream_interval_ms=new_config.interval_ms,
                mode=new_config.mode,
            )
        ).start(str(DATASETS_DIR / filename))
        _log.info("started collecting")

        st.session_state.cap_log_q = q
        st.session_state.cap_log_lines = []
        st.session_state.cap_collector = collector
        st.session_state.cap_handler = handler
        st.session_state.cap_running = True
        st.session_state.cap_filename = filename
        st.rerun()

    if stop_clicked and st.session_state.cap_running:
        _log.info("stopped collecting")
        st.session_state.cap_collector.stop()
        detach_log(st.session_state.cap_handler)
        drain(st.session_state.cap_log_q, st.session_state.cap_log_lines)
        st.session_state.cap_running = False
        st.session_state.cap_collector = None
        st.session_state.cap_handler = None
        st.rerun()

    # Auto-stop when collector exits on its own
    if st.session_state.cap_running and st.session_state.cap_collector:
        if not st.session_state.cap_collector.is_alive:
            detach_log(st.session_state.cap_handler)
            drain(st.session_state.cap_log_q, st.session_state.cap_log_lines)
            st.session_state.cap_running = False
            st.session_state.cap_collector = None
            st.session_state.cap_handler = None
            st.rerun()

    if st.session_state.cap_filename:
        st.caption(f"Output: datasets/{st.session_state.cap_filename}")

    st.subheader("Log Output")
    log_area("cap_log_area", st.session_state.cap_log_lines)

    if st.session_state.cap_running:
        time.sleep(0.5)
        st.rerun()
