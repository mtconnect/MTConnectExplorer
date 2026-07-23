import inspect
import logging
import os
import queue
import re
import threading
from pathlib import Path
from typing import Dict, List, Literal, Optional

import streamlit as st
from pydantic import BaseModel, ConfigDict, Field

from src.agent import AgentConfig, AgentAdapter
from src.client import (
    MtConnectClientConfiguration,
    MtConnectDataCollector,
    MtConnectDataCollectorConfiguration,
    MtConnectExplorer,
)
from src.shdr import ShdrDataPlayer
from src.simulator import EquipmentType, EquipmentSimulator

DATASETS_DIR = Path("datasets")
_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# ---------------------------------------------------------------------------
# Logging setup — mirrors src/main.py Config class and config.json logic
# ---------------------------------------------------------------------------
class _Config(BaseModel):
    log_level: Literal["debug", "info", "warning", "error", "critical"] = Field("info")

    def get_log_level_value(self) -> int:
        return logging._nameToLevel[self.log_level.upper()]


def _strip_json_comments(text: str) -> str:
    """Strip // line and /* */ block comments from JSON text so config.json can
    be commented like JSONC. Comment sequences inside string values (e.g. a
    "http://..." URL) are preserved: string literals are matched first and kept."""
    return re.sub(
        r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
        lambda m: m.group(0) if m.group(0).startswith('"') else "",
        text,
        flags=re.DOTALL,
    )


_log_level = (
    _Config.model_validate_json(
        _strip_json_comments(Path("config.json").read_text())
    ).get_log_level_value()
    if os.path.isfile("config.json")
    else _Config().get_log_level_value()
)
logging.basicConfig(level=_log_level, format=_LOG_FMT)
logging.getLogger("src").setLevel(_log_level)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.queue = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


def attach_log(q: queue.Queue) -> _QueueHandler:
    h = _QueueHandler(q)
    h.setFormatter(logging.Formatter(_LOG_FMT))
    logging.getLogger("src").addHandler(h)
    return h


def detach_log(h: Optional[_QueueHandler]) -> None:
    if h is not None:
        logging.getLogger("src").removeHandler(h)


# A play/capture session tags its worker/collector threads as "<label>\x1f<subname>"
# (via session_thread_name); the filter below recovers <label> so a single
# consolidated log can show which session each line came from.
_SESSION_THREAD_SEP = "\x1f"
_SESSION_LOG_FMT = "%(asctime)s [%(levelname)s] [%(session)s] %(message)s"


def session_thread_name(label: str, subname: str) -> str:
    """Encode a session label into a thread name so its log records can be tagged."""
    return f"{label}{_SESSION_THREAD_SEP}{subname}"


def agent_url(port: int) -> str:
    """Base URL of a locally-running MTConnect agent on the given port."""
    return f"http://localhost:{port}/"


class _SessionLogFilter(logging.Filter):
    """Annotate each record with the session label encoded in its thread name
    (text before the separator). When ``sessions`` is given, only records whose
    label matches one of those sessions are accepted — this isolates a page's
    consolidated log from other pages that share the 'src' logger."""

    def __init__(self, sessions: Optional[dict] = None) -> None:
        super().__init__()
        self._sessions = sessions

    def filter(self, record: logging.LogRecord) -> bool:
        tname = record.threadName or ""
        label = tname.split(_SESSION_THREAD_SEP, 1)[0] if _SESSION_THREAD_SEP in tname else "-"
        record.session = label
        if self._sessions is None:
            return True
        return any(getattr(s, "label", None) == label for s in self._sessions.values())


def attach_session_log(q: queue.Queue, sessions: Optional[dict] = None) -> _QueueHandler:
    """Attach a consolidated log handler that tags each line with the originating
    session label (encoded in the emitting thread's name). Pass the page's live
    ``sessions`` dict to only capture that page's sessions."""
    h = _QueueHandler(q)
    h.setFormatter(logging.Formatter(_SESSION_LOG_FMT))
    h.addFilter(_SessionLogFilter(sessions))
    logging.getLogger("src").addHandler(h)
    return h


def drain(q: queue.Queue, lines: list) -> None:
    while True:
        try:
            lines.append(q.get_nowait())
        except queue.Empty:
            break


_LOG_AREA_CSS = """
<style>
div[data-testid="stTextArea"] textarea {
    min-height: 200px !important;
    height: calc(100vh - 480px) !important;
}
</style>
"""


def log_area(key: str, lines: list) -> None:
    text = "\n".join(lines)
    # Update the widget's content via session state only. Passing both value=
    # and key= while also writing session_state[key] triggers a Streamlit
    # warning ("created with a default value but also had its value set via the
    # Session State API"); setting session_state alone updates it each rerun.
    st.session_state[key] = text
    st.text_area("Log", height=200, key=key, label_visibility="collapsed", disabled=True)
    st.markdown(_LOG_AREA_CSS, unsafe_allow_html=True)
    # Include len(lines) so this HTML differs on each update, forcing Streamlit
    # to re-render the iframe and re-execute the script. Double rAF fires after
    # React has committed its DOM changes, ensuring scrollTop is set last.
    st.iframe(
        f"""<script>
        /* n={len(lines)} */
        window.parent.requestAnimationFrame(function() {{
            window.parent.requestAnimationFrame(function() {{
                var areas = window.parent.document.querySelectorAll('textarea');
                areas.forEach(function(a) {{ a.scrollTop = a.scrollHeight; }});
            }});
        }});
        </script>""",
        height=1,
    )


# ===========================================================================
# Session-state models
#
# Session data is organized into a single shared model (GlobalState) plus one
# model per page. Each is stored under a single st.session_state key:
#
#     st.session_state.global_state    -> GlobalState        (shared by all pages)
#     st.session_state.explore_state   -> ExplorePageState
#     st.session_state.capture_state   -> CapturePageState
#     st.session_state.datasets_state  -> DatasetsPageState
#     st.session_state.play_state      -> PlayPageState
#
# Because pydantic models are ordinary mutable objects held by reference in
# st.session_state, a page pulls its model once at load (via the get_* helpers)
# and mutates fields in place; those mutations persist across reruns without
# writing back to st.session_state explicitly.
# ===========================================================================


# ---------------------------------------------------------------------------
# Shared (cross-page) state
# ---------------------------------------------------------------------------
class GlobalState(BaseModel):
    """Session state shared across all pages."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base_url: str = Field(
        "https://demo.mtconnect.org/",
        description="MTConnect agent base URL, shared by the Explore and Capture pages.",
    )


# ---------------------------------------------------------------------------
# Explore page
# ---------------------------------------------------------------------------
class ExplorePageState(BaseModel):
    """Session state for the Explore page."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    running: bool = Field(
        False,
        description="Whether the explorer is currently connected and streaming.",
    )
    explorer: Optional[MtConnectExplorer] = Field(
        None,
        description="Active MtConnectExplorer polling the agent, or None when stopped.",
    )
    selected_id: Optional[str] = Field(
        None,
        description="Element id of the node currently selected in the device tree.",
    )
    expanded: Optional[List[str]] = Field(
        None,
        description="Element ids of the expanded tree nodes; None until first initialized.",
    )
    device_filter: Optional[str] = Field(
        None,
        description="Label of the device selected in the Device dropdown.",
    )
    node_filter: str = Field(
        "",
        description="Search string used to filter device-tree nodes by name or id.",
    )
    hist_window_val: int = Field(
        5,
        description="Duration value for the selected node's history chart time window.",
    )
    hist_window_unit: str = Field(
        "minutes",
        description="Duration unit (seconds/minutes/hours/days) for the history chart window.",
    )
    pinned_items: List[str] = Field(
        default_factory=list,
        description="DataItem ids pinned to the Pinned Plots section.",
    )
    pin_window_val: int = Field(
        5,
        description="Duration value for the Pinned Plots time window.",
    )
    pin_window_unit: str = Field(
        "minutes",
        description="Duration unit (seconds/minutes/hours/days) for the Pinned Plots window.",
    )
    pin_auto_refresh: bool = Field(
        True,
        description="Whether Pinned Plots auto-refresh with live data. When off, the "
        "plots freeze so data can be examined without them moving.",
    )
    last_error: Optional[str] = Field(
        None,
        description="Most recent connection error message, shown after the explorer stops.",
    )
    cfg_interval_ms: int = Field(
        1000,
        description="Configured polling/stream interval in milliseconds.",
    )
    cfg_mode: str = Field(
        "stream",
        description="Configured data collection mode: stream, sample, or current.",
    )


# ---------------------------------------------------------------------------
# Capture page
# ---------------------------------------------------------------------------
class CaptureConfig(BaseModel):
    """Configuration captured from the Capture page form (base_url is global)."""

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


class CaptureSession(BaseModel):
    """A single in-flight data capture (its own collector writing one dataset)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str = Field(
        description="Unique identifier for this capture session.",
    )
    label: str = Field(
        description="Human-readable label (filename | base URL) used to tag log lines.",
    )
    filename: str = Field(
        description="Dataset filename being written by this capture.",
    )
    base_url: str = Field(
        description="MTConnect agent base URL this capture is collecting from.",
    )
    config: CaptureConfig = Field(
        description="Capture configuration applied to this session.",
    )
    collector: Optional[MtConnectDataCollector] = Field(
        None,
        description="Active MtConnectDataCollector for this session (owns its own thread).",
    )


class CapturePageState(BaseModel):
    """Session state for the Capture page (supports multiple simultaneous captures)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: CaptureConfig = Field(
        default_factory=CaptureConfig,
        description="Default/last-used configuration shown in the launch form.",
    )
    sessions: Dict[str, CaptureSession] = Field(
        default_factory=dict,
        description="Active capture sessions, keyed by session id.",
    )
    next_id: int = Field(
        1,
        description="Monotonic counter used to assign unique session ids.",
    )
    log_q: queue.Queue = Field(
        default_factory=queue.Queue,
        description="Thread-safe queue receiving consolidated log records from all captures.",
    )
    log_lines: List[str] = Field(
        default_factory=list,
        description="Accumulated consolidated log lines shown in the Log Output area.",
    )
    handler: Optional[logging.Handler] = Field(
        None,
        description="Consolidated logging handler, attached while any capture is active.",
    )


# ---------------------------------------------------------------------------
# Datasets page
# ---------------------------------------------------------------------------
class DatasetsPageState(BaseModel):
    """Session state for the Datasets page (currently stateless)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


# ---------------------------------------------------------------------------
# Play page
# ---------------------------------------------------------------------------
class PlayDeviceAdapter(BaseModel):
    """One device selected for playback and the SHDR adapter that will feed it.

    A Play session runs one agent fronting several of these — one SHDR adapter
    (its own host/port) per selected device."""

    device_name: str = Field(
        description="Device name (from the dataset) to replay through this adapter",
    )
    player_host: str = Field(
        AgentAdapter.model_fields["host"].default,
        description="SHDR adapter host address",
    )
    player_port: int = Field(
        AgentAdapter.model_fields["port"].default,
        description="SHDR adapter port (unique across all running Play/Simulator sessions)",
    )


class PlayConfig(BaseModel):
    """Configuration captured from the Play page form."""

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
    devices: List["PlayDeviceAdapter"] = Field(
        default_factory=list,
        description="Selected devices to replay; one SHDR adapter is spun up per entry "
        "and the single agent connects to all of them.",
    )
    re_timestamp: bool = Field(
        True,
        description="Re-stamp observations with the current time on replay so the "
        "data appears live; when disabled, original recorded timestamps are kept",
    )
    loop: bool = Field(
        True,
        description="Repeat playback from the start once the dataset is exhausted",
    )


class PlaybackSession(BaseModel):
    """A single in-flight dataset playback (its own agent, SHDR adapter, worker)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str = Field(
        description="Unique identifier for this playback session.",
    )
    label: str = Field(
        description="Human-readable label (dataset | device | agent URL) used to tag log lines.",
    )
    dataset_file: str = Field(
        description="Dataset filename being played back.",
    )
    config: PlayConfig = Field(
        description="Playback configuration applied to this session.",
    )
    worker: Optional[threading.Thread] = Field(
        None,
        description="Background thread running this session's agent and dataset player.",
    )
    stop_event: Optional[threading.Event] = Field(
        None,
        description="Event used to signal this session's worker thread to stop.",
    )
    started: Optional[threading.Event] = Field(
        None,
        description="Set by the worker once the agent has connected to every SHDR "
        "adapter and playback has begun; drives the 'starting' vs 'playing' status.",
    )


class PlayPageState(BaseModel):
    """Session state for the Play page (supports multiple simultaneous playbacks)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: PlayConfig = Field(
        default_factory=PlayConfig,
        description="Default/last-used configuration shown in the launch form.",
    )
    sessions: Dict[str, PlaybackSession] = Field(
        default_factory=dict,
        description="Active playback sessions, keyed by session id.",
    )
    next_id: int = Field(
        1,
        description="Monotonic counter used to assign unique session ids.",
    )
    log_q: queue.Queue = Field(
        default_factory=queue.Queue,
        description="Thread-safe queue receiving consolidated log records from all sessions.",
    )
    log_lines: List[str] = Field(
        default_factory=list,
        description="Accumulated consolidated log lines shown in the Log Output area.",
    )
    handler: Optional[logging.Handler] = Field(
        None,
        description="Consolidated logging handler, attached while any session is active.",
    )


# ---------------------------------------------------------------------------
# Simulator page
#
# Like the Play page, but the data source is generated on the fly (a canned
# routine for a virtual mill or robot) instead of a recorded dataset. Each
# session owns its own agent + SHDR adapter + EquipmentSimulator worker.
# ---------------------------------------------------------------------------
class SimulatorConfig(BaseModel):
    """Configuration captured from the Simulator page form."""

    equipment_type: str = Field(
        EquipmentType.MILL_3AXIS.value,
        description="Equipment type key (see EquipmentType): MILL_3AXIS, MILL_5AXIS_TRUNNION, or ROBOT_6DOF.",
    )
    device_name: str = Field(
        "",
        description="MTConnect device name for the simulated equipment.",
    )
    routine: str = Field(
        "",
        description="Label of the selected canned routine.",
    )
    tick_ms: int = Field(
        10,
        description="Interval in milliseconds at which generated observations are fed "
        "to the SHDR adapter.",
    )
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


class SimulatorSession(BaseModel):
    """A single in-flight simulation (its own agent, SHDR adapter, worker)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str = Field(
        description="Unique identifier for this simulation session.",
    )
    label: str = Field(
        description="Human-readable label (equipment | device | agent URL) used to tag log lines.",
    )
    config: SimulatorConfig = Field(
        description="Simulation configuration applied to this session.",
    )
    simulator: Optional[EquipmentSimulator] = Field(
        None,
        description="The EquipmentSimulator for this session (owns the SHDR adapter + routine). "
        "Held here so the 3D visualization can read its routine spec and start time.",
    )
    worker: Optional[threading.Thread] = Field(
        None,
        description="Background thread running this session's agent and simulator.",
    )
    stop_event: Optional[threading.Event] = Field(
        None,
        description="Event used to signal this session's worker thread to stop.",
    )


class SimulatorPageState(BaseModel):
    """Session state for the Simulator page (supports multiple simultaneous simulations)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: SimulatorConfig = Field(
        default_factory=SimulatorConfig,
        description="Default/last-used configuration shown in the launch form.",
    )
    sessions: Dict[str, SimulatorSession] = Field(
        default_factory=dict,
        description="Active simulation sessions, keyed by session id.",
    )
    next_id: int = Field(
        1,
        description="Monotonic counter used to assign unique session ids.",
    )
    log_q: queue.Queue = Field(
        default_factory=queue.Queue,
        description="Thread-safe queue receiving consolidated log records from all sessions.",
    )
    log_lines: List[str] = Field(
        default_factory=list,
        description="Accumulated consolidated log lines shown in the Log Output area.",
    )
    handler: Optional[logging.Handler] = Field(
        None,
        description="Consolidated logging handler, attached while any session is active.",
    )


# ---------------------------------------------------------------------------
# Shared, process-global state store
#
# st.session_state is per-session, so it does not survive browser reloads and is
# not shared between users. Instead, the state models are held in a single dict
# returned by st.cache_resource, which hands the SAME instance to every session
# for the lifetime of the server process. This makes all state:
#   * persistent across browser reloads (a fresh session re-reads the same store)
#   * shared across all connected users (one global session)
# It is in-memory only — a server restart (or st.cache_resource.clear()) resets it.
#
# Because the store is shared by concurrent sessions and background threads, an
# RLock guards lazy initialization; pages should also wrap Start/Stop logic in
# `with get_state_lock():` so two users cannot launch duplicate worker threads.
# ---------------------------------------------------------------------------
@st.cache_resource
def _shared_store() -> dict:
    """Return the process-global, in-memory state store (one instance per server)."""
    return {"_lock": threading.RLock()}


def get_state_lock() -> threading.RLock:
    """Reentrant lock guarding shared-state init and page Start/Stop sections."""
    return _shared_store()["_lock"]


def _get_shared(key: str, factory):
    """Pull-or-initialize the shared model stored under ``key`` (thread-safe)."""
    store = _shared_store()
    if key not in store:
        with store["_lock"]:
            if key not in store:
                store[key] = factory()
    return store[key]


# ---------------------------------------------------------------------------
# Accessors — pull-or-initialize the shared model for each scope
# ---------------------------------------------------------------------------
def get_global_state() -> GlobalState:
    return _get_shared("global_state", GlobalState)


def get_explore_state() -> ExplorePageState:
    return _get_shared("explore_state", ExplorePageState)


def get_capture_state() -> CapturePageState:
    return _get_shared("capture_state", CapturePageState)


def get_datasets_state() -> DatasetsPageState:
    return _get_shared("datasets_state", DatasetsPageState)


def get_play_state() -> PlayPageState:
    return _get_shared("play_state", PlayPageState)


def get_simulator_state() -> SimulatorPageState:
    return _get_shared("simulator_state", SimulatorPageState)


# ---------------------------------------------------------------------------
# Cross-page helpers — Play and Simulator both spin up local agents, so ports
# must be de-conflicted across both and running agents surfaced app-wide.
# ---------------------------------------------------------------------------
def all_used_ports() -> set:
    """Agent + SHDR ports in use by every running Play and Simulator session."""
    used: set = set()
    # Play sessions front one agent with N SHDR adapters (one per device).
    for s in get_play_state().sessions.values():
        used.add(s.config.agent_port)
        for d in s.config.devices:
            used.add(d.player_port)
    # Simulator sessions have a single SHDR adapter.
    for s in get_simulator_state().sessions.values():
        used.add(s.config.agent_port)
        used.add(s.config.player_port)
    return used


def running_agent_urls() -> List[str]:
    """Base URLs of all locally-running agents (Play + Simulator), de-duplicated."""
    urls: List[str] = []
    for state in (get_play_state(), get_simulator_state()):
        for s in state.sessions.values():
            u = agent_url(s.config.agent_port)
            if u not in urls:
                urls.append(u)
    return urls
