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


def _fmt_time(seconds: float) -> str:
    """Seconds -> M:SS or H:MM:SS timecode."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@st.fragment(run_every=1.0)
def _scrub_fragment(sid: str, looping: bool, re_timestamp: bool) -> None:
    """Per-session scrub bar.

    Each tick re-fetches the playhead origin and computes the live position
    (``server_time - pass_start_epoch``, both server-clock, so it advances
    tick-to-tick with no client clock math), rounded to the 1 s step.

    Seek detection avoids ``on_change`` (its programmatic-write echo caused a
    feedback loop of seek-to-current every tick). Instead we compare the
    slider's committed value against the exact value we last wrote: a tick with
    no user interaction reads back what we set (advance the thumb, no seek); a
    drag reads back the user's value (commit a seek). Rounding both to the step
    keeps quantization from masquerading as a drag.

    After a drag we mint a FRESH slider key (bump ``gen``). A user interaction
    leaves the widget with a *sticky* frontend value that a ``run_every`` auto-rerun
    re-asserts each tick — so without this, our advancing programmatic writes keep
    diverging from that pinned value and re-fire the seek every tick (playhead
    stuck, data re-scanning between snapshots). A new key drops the sticky value so
    subsequent writes advance the thumb like normal playback.

    An **echo guard** keeps a *stale widget echo* from masquerading as a drag
    (diagnosed 2026-08: a full-page rerun racing a fragment tick makes the browser
    re-assert the PREVIOUSLY rendered value over our fresh write — always exactly
    1-2 steps behind — which met the divergence threshold and fired a spurious
    backward seek; each seek then re-emitted up to snapshot_interval_s of data to
    the agent). The guard: remember the last few values this fragment wrote; a
    "diverged" committed value matching one of them is our own stale echo, never a
    drag (a drag lands on a value we did NOT just write). Swallowed, no seek.

    A non-echo divergence seeks IMMEDIATELY, on first sighting. Do not add a
    persist-N-ticks confirmation here: a single click/drag's committed value
    survives exactly ONE tick (the next programmatic write reclaims the widget —
    the frontend does not re-assert it), so any multi-tick requirement silently
    drops real single clicks (log-verified 2026-08; only double-clicks survived).

    Known trade-off: a backward click landing on one of the last 4 written values
    (≤4 s back) is swallowed as an echo; clicking anywhere ≥5 s away seeks
    instantly. Ring depth 4 = observed max echo staleness (2 steps) + margin.

    Disabled until running, and when Re-Timestamp is off (seeking re-emits data,
    which needs the re-stamped monotonic timestamps)."""
    gen_key = f"play_seek_gen_{sid}"
    set_key = f"play_seek_set_{sid}"
    ring_key = f"play_seek_ring_{sid}"      # last few values we wrote (echo check)
    gen = int(st.session_state.get(gen_key, 0))
    key = f"play_seek_{sid}_{gen}"
    try:
        prog = get_client().play_progress(sid)
    except ApiError:
        return  # session gone/purged; the page watcher will drop the row

    duration = prog.duration or 0.0
    epoch = prog.pass_start_epoch
    running = epoch is not None and duration > 0
    ready = running and re_timestamp
    paused = bool(getattr(prog, "paused", False))

    live = 0.0
    if running:
        if paused and prog.position is not None:
            # Paused: pass_start_epoch is frozen, so the live formula would drift —
            # hold the thumb at the authoritative frozen position instead.
            live = prog.position
        else:
            live = prog.server_time.timestamp() - epoch
            if looping:
                live = live % duration
        live = min(max(live, 0.0), duration)
    live = float(round(live))

    committed = st.session_state.get(key)
    last_set = st.session_state.get(set_key)
    ring = list(st.session_state.get(ring_key, []))
    diverged = (ready and committed is not None and last_set is not None
                and abs(committed - last_set) >= 1.0)
    display = live
    if diverged:
        if any(abs(committed - v) < 0.5 for v in ring):
            # Echo guard: a value WE wrote in the last few ticks bounced back late
            # (page-rerun race). Not a drag — swallow it and keep advancing.
            pass
        else:
            # A value we did not recently write: a genuine click/drag. Seek NOW —
            # it survives only this tick (see docstring). Show where they dropped
            # it, and mint a fresh key (bump gen) so any sticky value is discarded
            # — otherwise an auto-rerun could re-assert it and re-fire this seek.
            try:
                get_client().play_seek(sid, float(committed))
            except ApiError as ex:
                get_feed("play").post_ui(f"seek failed: {ex.message}")
            display = float(committed)
            gen += 1
            st.session_state[gen_key] = gen
            key = f"play_seek_{sid}_{gen}"

    # Pause/Resume toggle beside the scrub bar. The label follows the backend's
    # paused flag (so it's correct across browser sessions), and pause works in any
    # timestamp mode — unlike seek, it only holds the playhead.
    pause_col, slider_col = st.columns([1, 6], vertical_alignment="center")
    with pause_col:
        btn_label = "Resume" if paused else "Pause"
        if st.button(
            btn_label, key=f"play_pause_{sid}", disabled=not running, width="stretch"
        ):
            try:
                if paused:
                    get_client().play_resume(sid)
                else:
                    get_client().play_pause(sid)
            except ApiError as ex:
                get_feed("play").post_ui(f"{btn_label.lower()} failed: {ex.message}")
            st.rerun(scope="fragment")

    with slider_col:
        # Write-before-create (the log_area technique): set the value via
        # session_state alone (no value= arg) so it raises no warning. set_key
        # records exactly what we wrote, for next tick's drag check; the ring
        # keeps the last few writes so a late echo of any of them is recognized.
        ring.append(display)
        st.session_state[ring_key] = ring[-4:]
        st.session_state[set_key] = display
        st.session_state[key] = display
        st.slider(
            "Seek",
            min_value=0.0,
            max_value=float(duration) if duration > 0 else 1.0,
            step=1.0,
            key=key,
            label_visibility="collapsed",
            disabled=not ready,
            help=None if re_timestamp else "Scrubbing requires Re-Timestamp mode.",
        )
    caption = f"{_fmt_time(display)} / {_fmt_time(duration)}"
    if not running:
        caption += "  ·  starting…"
    elif paused:
        caption += "  ·  paused"
    elif not re_timestamp:
        caption += "  ·  scrubbing needs Re-Timestamp"
    st.caption(caption)


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
                # Scrub bar under the row: live playhead + drag-to-seek. Its own
                # ~1s fragment advances the thumb; a full-page rerun is never
                # triggered by playback progress (position is not in the
                # fingerprint), so the form and other rows stay put.
                _scrub_fragment(sess.id, sess.config.loop, sess.config.re_timestamp)
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

        snapshot_interval = st.number_input(
            "Snapshot Interval (s)",
            value=float(play_config.snapshot_interval_s),
            min_value=1.0, step=1.0,
            key="play_snapshot_interval",
            help="Seek keyframe cadence. A full-state snapshot is indexed every this "
            "many dataset seconds at load, so scrubbing jumps to the nearest snapshot "
            "and replays at most this many seconds of data (rather than from the "
            "start). Smaller = faster seeks, but more memory and a longer pre-scan. "
            "Fixed once playback starts.",
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
                    snapshot_interval_s=float(snapshot_interval),
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
                        snapshot_interval_s=float(snapshot_interval),
                    )
                    state.gen += 1
                st.rerun()

    # --- Log Output (full width) ---
    st.subheader("Log Output")
    _log_output_fragment()

    # Keep the page in sync with API-driven changes (change-triggered rerun —
    # a quiet backend never disturbs the form or an in-progress device edit).
    _watch_backend_fragment()
