import streamlit as st

from mtconnect_explorer_client import ApiError
from mtconnect_explorer_client.schemas import CaptureCreate

from src.pages.api import get_client, get_feed
from src.pages.core import (
    CaptureConfig,
    GlobalState,
    get_capture_state,
    get_global_state,
    get_state_lock,
    log_area,
)


def _backend_fingerprint() -> tuple:
    """What this page renders from the backend: capture sessions (id, status)
    plus the running-agent quick-fill URLs."""
    client = get_client()
    return (
        tuple(sorted((s.id, s.status) for s in client.capture_list())),
        tuple(sorted(a.base_url for a in client.agents_list())),
    )


@st.fragment(run_every=1.0)
def _watch_backend_fragment() -> None:
    """The API can change sessions behind this page's back — poll the backend
    and trigger a full-page rerun only when what the page rendered has actually
    changed, so idle pages pick up API-driven starts/stops without constant
    rerun churn."""
    try:
        current = _backend_fingerprint()
    except ApiError:
        return
    if current != st.session_state.get("cap_backend_fp"):
        st.rerun(scope="app")


@st.fragment(run_every=0.5)
def _log_output_fragment() -> None:
    """Live-refresh the consolidated log via a fragment (partial) rerun."""
    log_area("cap_log_area", get_feed("capture").lines)


def capture_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Capture")

    client = get_client()
    feed = get_feed("capture")
    global_state = get_global_state()
    state = get_capture_state()

    # Auto-delete terminal sessions (finished/error) so the grid reaps
    # instantly, exactly like the pre-API behavior.
    sessions = []
    try:
        for sess in client.capture_list():
            if sess.status in ("finished", "error"):
                try:
                    client.capture_stop(sess.id)
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
        st.session_state["cap_backend_fp"] = _backend_fingerprint()
    except ApiError:
        pass

    config: CaptureConfig = state.config

    # Pre-create both sections as stable top-level containers (always, in the
    # same order) so the form's columns are never reconciled against the
    # conditionally-rendered Capturing columns — that mismatch caused the form
    # to ghost/duplicate beside the grid right after launching.
    capturing_container = st.container()
    form_container = st.container()

    # --- Capturing ---
    if sessions:
        with capturing_container:
            head_l, head_r = st.columns([3, 1])
            head_l.subheader("Capturing")
            with head_r:
                if st.button("Stop All", width="stretch"):
                    try:
                        client.capture_stop_all()
                    except ApiError as ex:
                        feed.post_ui(f"stop all failed: {ex.message}")
                    st.rerun()

            h = st.columns([4, 3, 2, 2, 1])
            h[0].markdown("**Dataset**")
            h[1].markdown("**Base URL**")
            h[2].markdown("**Mode**")
            h[3].markdown("**Status**")
            h[4].markdown("**Stop**")
            for sess in sessions:
                c = st.columns([4, 3, 2, 2, 1])
                c[0].write(sess.config.filename)
                c[1].write(sess.config.base_url)
                c[2].write(sess.config.mode)
                c[3].write("stopping" if sess.status == "stopping" else "capturing")
                with c[4]:
                    if st.button("Stop", key=f"cap_stop_{sess.id}", width="stretch"):
                        try:
                            client.capture_stop(sess.id)
                        except ApiError as ex:
                            feed.post_ui(f"stop failed: {ex.message}")
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
        try:
            playing_urls = [a.base_url for a in client.agents_list()]
        except ApiError:
            playing_urls = []
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
                state.config = CaptureConfig(
                    interval_ms=int(stream_interval_ms),
                    mode=str(mode),
                )
            try:
                client.capture_start(CaptureCreate(
                    base_url=str(base_url),
                    mode=state.config.mode,
                    interval_ms=state.config.interval_ms,
                    timeout_s=state.config.timeout,
                    chunk_size=state.config.chunk_size,
                ))
            except ApiError as ex:
                feed.post_ui(f"start capture failed: {ex.message}")
                st.error(f"Failed to start capture: {ex.message}")
            except Exception as ex:
                st.error(f"Failed to start capture: {ex}")
            else:
                st.rerun()

    # --- Log Output (full width) ---
    st.subheader("Log Output")
    _log_output_fragment()

    # Keep the page in sync with API-driven changes (change-triggered rerun —
    # a quiet backend never disturbs the form).
    _watch_backend_fragment()
