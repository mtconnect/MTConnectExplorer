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
    get_state_lock,
    log_area,
    running_agent_urls,
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
                if st.button("Stop All", width="stretch"):
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
                    if st.button("Stop", key=f"cap_stop_{sid}", width="stretch"):
                        with get_state_lock():
                            _remove_session(state, sid)
                        st.rerun()
            st.divider()

    # --- Start Capture (kept narrow; it looks best at this width) ---
    _, form_col, _ = form_container.columns([1, 2, 1])
    with form_col:
        st.subheader("Start Capture")
        # Base URL is a plain text field so a manually typed value is kept exactly
        # as entered — tab, enter, or clicking Start all commit it. Currently-
        # running local agents (Play and Simulator sessions) are offered as
        # one-click quick-fills below.
        playing_urls = running_agent_urls()
        default_url = GlobalState.model_fields["base_url"].default
        if "cap_base_url" not in st.session_state:
            st.session_state["cap_base_url"] = global_state.base_url or default_url

        def _use_base_url(url: str) -> None:
            st.session_state["cap_base_url"] = url

        base_url = st.text_input(
            "Base URL",
            key="cap_base_url",
            placeholder=default_url,
        )
        # Quick-fill buttons for the demo server and any currently-running local
        # agents, one per column in a single row right under the field; clicking
        # one populates the Base URL field above.
        quick_urls = [u for u in dict.fromkeys([default_url, *playing_urls]) if u]
        if quick_urls:
            qcols = st.columns(len(quick_urls))
            for i, u in enumerate(quick_urls):
                qcols[i].button(u, key=f"cap_url_quick_{i}", width="stretch",
                                on_click=_use_base_url, args=(u,))
        # Interval default depends on the target. The public demo server is
        # shared, so it defaults to a courteous 1000 ms and is floored there for
        # every mode (you can set a higher interval, never a lower one) to avoid
        # accidentally hammering it. Any other agent (typically a local one) can
        # be polled far faster, so the default depends on the mode; that value is
        # only a starting point and the user can override it.
        _MODE_DEFAULT_INTERVAL_MS = {"stream": 10, "sample": 50, "current": 200}
        _DEMO_MIN_INTERVAL_MS = 1000
        demo_url = default_url.strip().rstrip("/")
        is_demo = str(base_url).strip().rstrip("/") == demo_url

        c1, c2 = st.columns(2)
        # Mode (left) is created first: the interval default reads its value.
        with c1:
            _modes = ["stream", "sample", "current"]
            mode = st.selectbox(
                "Mode", _modes,
                index=_modes.index(config.mode) if config.mode in _modes else 0,
                key="cap_mode",
                filter_mode=None,  # Disable search/text input
            )
        with c2:
            # The key varies with target/mode so switching either re-seeds the
            # default — Streamlit ignores `value` once a keyed widget has state.
            if is_demo:
                interval_value = interval_min = _DEMO_MIN_INTERVAL_MS
                interval_key = "cap_interval_demo"
            else:
                interval_value = _MODE_DEFAULT_INTERVAL_MS.get(mode, 10)
                interval_min = 1
                interval_key = f"cap_interval_{mode}"
            stream_interval_ms = st.number_input(
                "Interval (ms)", value=interval_value, min_value=interval_min, step=1,
                key=interval_key,
                help=("The public demo server is shared: 1000 ms is the minimum "
                      "(you can set a higher interval) to avoid overloading it."
                      if is_demo else None),
            )

        start_clicked = st.button(
            "Start", disabled=not str(base_url).strip(),
            width="stretch", type="primary",
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
