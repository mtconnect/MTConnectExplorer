import logging
import re
import time
from datetime import datetime, timezone

import streamlit as st

from src.client import (
    MtConnectClientConfiguration,
    MtConnectDataCollector,
    MtConnectDataCollectorConfiguration,
)
from src.pages.core import (
    DATASETS_DIR,
    CaptureConfig,
    CaptureSession,
    CapturePageState,
    GlobalState,
    agent_url,
    attach_session_log,
    detach_log,
    drain,
    get_capture_state,
    get_global_state,
    get_play_state,
    get_state_lock,
    log_area,
    session_thread_name,
)


_log = logging.getLogger(__name__)


def _filename(base_url: str) -> str:
    cleaned = re.sub(
        r"_+", "_",
        re.sub(r"[^a-zA-Z0-9._-]", "_", base_url.replace("://", "_").rstrip("/"))
    ).strip("_")
    return f"{cleaned}-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}.jsonl"


def _unique_filename(base_url: str, state: CapturePageState) -> str:
    """A dataset filename for this capture that doesn't collide with an existing
    file or another in-progress capture."""
    active = {s.filename for s in state.sessions.values()}
    name = _filename(base_url)
    if not (DATASETS_DIR / name).exists() and name not in active:
        return name
    stem, _, ext = name.rpartition(".")
    i = 2
    while True:
        candidate = f"{stem}-{i}.{ext}"
        if not (DATASETS_DIR / candidate).exists() and candidate not in active:
            return candidate
        i += 1


def _capture_label(filename: str, base_url: str) -> str:
    """Human-readable session label used to tag log lines and label the grid."""
    return f"{filename} | {base_url}"


def _sync_log_handler(state: CapturePageState) -> None:
    """Detach the consolidated log handler once no captures remain. Caller holds the lock."""
    if not state.sessions and state.handler is not None:
        detach_log(state.handler)
        state.handler = None


def _remove_session(state: CapturePageState, sid: str) -> None:
    """Stop a capture's collector and drop it. Caller holds the lock."""
    sess = state.sessions.get(sid)
    if sess is None:
        return
    if sess.collector is not None:
        try:
            sess.collector.stop()
        except Exception:
            pass
    state.sessions.pop(sid, None)
    _sync_log_handler(state)


def _reap_finished(state: CapturePageState) -> None:
    """Drop captures whose collector exited on its own. Caller holds the lock."""
    for sid, sess in list(state.sessions.items()):
        if sess.collector is None or not sess.collector.is_alive:
            state.sessions.pop(sid, None)
    _sync_log_handler(state)


def capture_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Capture")

    global_state = get_global_state()
    state = get_capture_state()

    drain(state.log_q, state.log_lines)

    # Reap captures whose collector has finished/died so the grid stays accurate.
    with get_state_lock():
        _reap_finished(state)

    config: CaptureConfig = state.config

    # Pre-create both sections as stable top-level containers (always, in the
    # same order) so the form's columns are never reconciled against the
    # conditionally-rendered Capturing columns — that mismatch caused the form
    # to ghost/duplicate beside the grid right after launching.
    capturing_container = st.container()
    form_container = st.container()

    # --- Capturing ---
    if state.sessions:
        with capturing_container:
            head_l, head_r = st.columns([3, 1])
            head_l.subheader("Capturing")
            with head_r:
                if st.button("Stop All", use_container_width=True):
                    with get_state_lock():
                        for sid in list(state.sessions):
                            _remove_session(state, sid)
                    st.rerun()

            h = st.columns([4, 3, 2, 2, 1])
            h[0].markdown("**Dataset**")
            h[1].markdown("**Base URL**")
            h[2].markdown("**Mode**")
            h[3].markdown("**Status**")
            h[4].markdown("**Stop**")
            for sid, sess in list(state.sessions.items()):
                c = st.columns([4, 3, 2, 2, 1])
                c[0].write(sess.filename)
                c[1].write(sess.base_url)
                c[2].write(sess.config.mode)
                alive = sess.collector is not None and sess.collector.is_alive
                c[3].write("capturing" if alive else "stopping")
                with c[4]:
                    if st.button("Stop", key=f"cap_stop_{sid}", use_container_width=True):
                        with get_state_lock():
                            _remove_session(state, sid)
                        st.rerun()
            st.divider()

    # --- Start Capture (kept narrow; it looks best at this width) ---
    _, form_col, _ = form_container.columns([1, 2, 1])
    with form_col:
        st.subheader("Start Capture")
        # Editable dropdown: free-text URL. The default demo URL is always first
        # and the default selection; only currently-playing agents follow as
        # quick options (so stopped playbacks don't linger in the list).
        play_state = get_play_state()
        playing_urls = [agent_url(s.config.agent_port) for s in play_state.sessions.values()]
        default_url = GlobalState.model_fields["base_url"].default
        base_url_options: list = []
        for u in [default_url, *playing_urls]:
            if u and u not in base_url_options:
                base_url_options.append(u)
        base_url = st.selectbox(
            "Base URL",
            base_url_options,
            index=0,
            accept_new_options=True,
            key="cap_base_url_select",
        )
        c1, c2 = st.columns(2)
        with c1:
            stream_interval_ms = st.number_input(
                "Interval (ms)", value=config.interval_ms, min_value=1, step=1,
                key="cap_stream_interval_ms",
            )
        with c2:
            _modes = ["stream", "sample", "current"]
            mode = st.selectbox(
                "Mode", _modes,
                index=_modes.index(config.mode) if config.mode in _modes else 0,
                key="cap_mode",
                filter_mode=None,  # Disable search/text input
            )

        start_clicked = st.button(
            "Start", disabled=not str(base_url).strip(),
            use_container_width=True, type="primary",
        )

        if start_clicked and str(base_url).strip():
            with get_state_lock():
                global_state.base_url = str(base_url)
                new_config = CaptureConfig(
                    interval_ms=int(stream_interval_ms),
                    mode=str(mode),
                )
                state.config = new_config

                DATASETS_DIR.mkdir(exist_ok=True)
                filename = _unique_filename(str(base_url), state)
                label = _capture_label(filename, str(base_url))

                if state.handler is None:
                    state.handler = attach_session_log(state.log_q, state.sessions)

                sid = str(state.next_id)
                state.next_id += 1
                try:
                    collector = MtConnectDataCollector(
                        MtConnectDataCollectorConfiguration(
                            configuration=MtConnectClientConfiguration(
                                base_url=str(base_url),
                                timeout=new_config.timeout,
                                chunk_size=new_config.chunk_size,
                            ),
                            stream_interval_ms=new_config.interval_ms,
                            mode=new_config.mode,
                        ),
                        thread_name=session_thread_name(label, "collector"),
                    ).start(str(DATASETS_DIR / filename))
                except Exception as ex:
                    _sync_log_handler(state)
                    st.error(f"Failed to start capture: {ex}")
                else:
                    state.sessions[sid] = CaptureSession(
                        id=sid,
                        label=label,
                        filename=filename,
                        base_url=str(base_url),
                        config=new_config,
                        collector=collector,
                    )
                    st.rerun()

    # --- Log Output (full width) ---
    st.subheader("Log Output")
    log_area("cap_log_area", state.log_lines)

    if state.sessions:
        time.sleep(0.5)
        st.rerun()
