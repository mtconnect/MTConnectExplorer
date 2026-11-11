"""Frontend view state and shared UI helpers.

Everything here is *view* state (form defaults, tree selections, log-pane
widgets). Domain state — sessions, datasets, ports, events — lives behind the
HTTP API and is reached through the lib client (see src/pages/api.py). The one
carve-out is Explore, which keeps a direct ``MtConnectExplorer`` (an MTConnect
agent is itself a REST API; see docs/api.md).
"""

import threading
from typing import List, Optional

import streamlit as st
from pydantic import BaseModel, ConfigDict, Field

from mtconnect_explorer_client.schemas import AgentSettings, CaptureCreate, PlayCreate, ShdrSettings

from src.client import MtConnectExplorer


def agent_url(port: int) -> str:
    """Base URL of a locally-running MTConnect agent on the given port."""
    return f"http://localhost:{port}/"


# ---------------------------------------------------------------------------
# Log pane
# ---------------------------------------------------------------------------
_LOG_AREA_CSS = """
<style>
div[data-testid="stTextArea"] textarea {
    min-height: 200px !important;
    height: calc(100vh - 480px) !important;
}
</style>
"""


def log_area(key: str, lines) -> None:
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
# View state is organized into a single shared model (GlobalState) plus one
# model per page, held in the process-global store below. Because pydantic
# models are ordinary mutable objects held by reference, a page pulls its
# model once at load (via the get_* helpers) and mutates fields in place.
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
    """Configuration captured from the Capture page form (base_url is global).
    Defaults mirror the API's CaptureCreate schema."""

    timeout: float = Field(
        CaptureCreate.model_fields["timeout_s"].default,
        description="Socket timeout in seconds for non-streaming requests",
    )
    chunk_size: int = Field(
        CaptureCreate.model_fields["chunk_size"].default,
        description="Read chunk size in bytes for streaming responses",
    )
    interval_ms: int = Field(
        CaptureCreate.model_fields["interval_ms"].default,
        description="Milliseconds between agent stream buffer checks",
    )
    mode: str = Field(
        CaptureCreate.model_fields["mode"].default,
        description="Data collection mode: stream, sample, or current",
    )


class CapturePageState(BaseModel):
    """View state for the Capture page (sessions live behind the API)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: CaptureConfig = Field(
        default_factory=CaptureConfig,
        description="Default/last-used configuration shown in the launch form.",
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
class PlayConfig(BaseModel):
    """Launch-form defaults for the Play page (defaults mirror AgentSettings)."""

    agent_port: int = Field(
        AgentSettings.model_fields["port"].default,
        description="MTConnect agent HTTP port",
    )
    mtconnect_version: str = Field(
        AgentSettings.model_fields["mtconnect_version"].default,
        description="MTConnect version string",
    )
    asset_buffer_size: int = Field(
        AgentSettings.model_fields["asset_buffer_size"].default,
        description="Agent asset buffer size",
    )
    buffer_size: int = Field(
        AgentSettings.model_fields["buffer_size"].default,
        description="Agent circular buffer size",
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
    snapshot_interval_s: float = Field(
        PlayCreate.model_fields["snapshot_interval_s"].default,
        description="Seek keyframe cadence (dataset seconds) for the scrub index",
    )


class PlayPageState(BaseModel):
    """View state for the Play page (sessions live behind the API)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: PlayConfig = Field(
        default_factory=PlayConfig,
        description="Default/last-used configuration shown in the launch form.",
    )
    gen: int = Field(
        1,
        description="Launch generation counter; embedded in port-widget keys so "
        "they re-seed to the next free ports after each launch.",
    )


# ---------------------------------------------------------------------------
# Simulator page
# ---------------------------------------------------------------------------
class SimulatorConfig(BaseModel):
    """Launch-form defaults for the Simulator page."""

    equipment_type: str = Field(
        "mill_3axis",
        description="Equipment type key from the GET /simulate/equipment catalog.",
    )
    device_name: str = Field(
        "",
        description="MTConnect device name for the simulated equipment.",
    )
    routine: str = Field(
        "",
        description="Label of the selected routine.",
    )
    interval_ms: int = Field(
        10,
        description="Interval in milliseconds at which generated observations are fed "
        "to the SHDR adapter.",
    )
    agent_port: int = Field(
        AgentSettings.model_fields["port"].default,
        description="MTConnect agent HTTP port",
    )
    mtconnect_version: str = Field(
        AgentSettings.model_fields["mtconnect_version"].default,
        description="MTConnect version string",
    )
    asset_buffer_size: int = Field(
        AgentSettings.model_fields["asset_buffer_size"].default,
        description="Agent asset buffer size",
    )
    buffer_size: int = Field(
        AgentSettings.model_fields["buffer_size"].default,
        description="Agent circular buffer size",
    )
    player_host: str = Field(
        ShdrSettings.model_fields["host"].default,
        description="SHDR adapter host address",
    )
    player_port: int = Field(
        ShdrSettings.model_fields["port"].default,
        description="SHDR adapter port",
    )


class SimulatorPageState(BaseModel):
    """View state for the Simulator page (sessions live behind the API)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: SimulatorConfig = Field(
        default_factory=SimulatorConfig,
        description="Default/last-used configuration shown in the launch form.",
    )
    gen: int = Field(
        1,
        description="Launch generation counter; embedded in port-widget keys so "
        "they re-seed to the next free ports after each launch.",
    )


# ---------------------------------------------------------------------------
# Shared, process-global state store
#
# st.session_state is per-session, so it does not survive browser reloads and is
# not shared between users. Instead, the state models are held in a single dict
# returned by st.cache_resource, which hands the SAME instance to every session
# for the lifetime of the server process. This makes all view state:
#   * persistent across browser reloads (a fresh session re-reads the same store)
#   * shared across all connected users (one global session)
# It is in-memory only — a server restart (or st.cache_resource.clear()) resets it.
#
# Because the store is shared by concurrent sessions and background threads, an
# RLock guards lazy initialization; pages should also wrap Start/Stop logic in
# `with get_state_lock():` so two users cannot race form-state updates.
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
