"""Play-scope models (docs/api.md §7)."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from .common import SessionInfo


class AgentSettings(BaseModel):
    """Bundled MTConnect agent settings. Defaults mirror the app's AgentConfig."""

    port: int = Field(5000, ge=1, le=65535, description="Agent HTTP port.")
    mtconnect_version: str = Field("2.7", description="MTConnect version string.")
    asset_buffer_size: int = Field(1024, ge=1, description="Agent asset buffer size.")
    buffer_size: int = Field(131072, ge=1, description="Agent circular buffer size.")


class PlayDevice(BaseModel):
    """One device selected for playback and the SHDR adapter that will feed it."""

    device_name: str = Field(min_length=1, description="Device name from the dataset.")
    shdr_host: str = Field("127.0.0.1", description="SHDR adapter host address.")
    shdr_port: int = Field(7878, ge=1, le=65535, description="SHDR adapter TCP port.")


class PlayCreate(BaseModel):
    """Body of POST /play/sessions."""

    dataset: str = Field(min_length=1, description="Dataset filename to replay.")
    devices: List[PlayDevice] = Field(
        min_length=1,
        description="Devices to replay; one SHDR adapter is spun up per entry.",
    )
    agent: AgentSettings = Field(
        default_factory=AgentSettings, description="Bundled agent settings."
    )
    re_timestamp: bool = Field(
        True,
        description="Re-stamp observations with the current time on replay so the "
        "data appears live; when false, original recorded timestamps are kept.",
    )
    loop: bool = Field(
        True, description="Repeat playback from the start once the dataset is exhausted."
    )
    snapshot_interval_s: float = Field(
        15.0,
        gt=0,
        description="Seek keyframe cadence in dataset seconds. A full-state snapshot "
        "is indexed every this-many seconds at load, so scrubbing replays only from "
        "the nearest snapshot (≤ this many seconds of deltas) instead of from the "
        "start. Smaller = faster seeks but more memory and a longer pre-scan.",
    )


class PlaySessionInfo(SessionInfo):
    """Play session envelope with a typed config echo."""

    config: PlayCreate


class PlayProgress(BaseModel):
    """GET /play/sessions/{id}/progress — the scrub bar's origin.

    The live playhead is computed client-side as ``now - pass_start_epoch``
    (clamped to ``duration``; wrapped by ``duration`` when ``looping``), so only
    this origin is transmitted — the worker republishes it on start, loop-wrap,
    and seek, not per frame. ``server_time`` lets the client correct for
    browser<->backend clock skew (same pattern as the 3D view's RoutineInfo)."""

    duration: float = Field(
        description="Total dataset span in seconds (the scrub range)."
    )
    pass_start_epoch: Optional[float] = Field(
        None,
        description="Unix time of the current pass's offset 0; null until running.",
    )
    looping: bool = Field(
        description="Whether playback loops (the UI wraps the playhead at duration)."
    )
    paused: bool = Field(
        False,
        description="Whether playback is paused. While paused, pass_start_epoch stops "
        "advancing and the UI holds the playhead at `position` instead of computing "
        "`server_time - pass_start_epoch`.",
    )
    position: Optional[float] = Field(
        None,
        description="Current playhead in seconds; authoritative while paused (the "
        "frozen value). Null until running.",
    )
    server_time: datetime = Field(
        description="Server clock at response time, for client clock-offset correction."
    )


class PlaySeek(BaseModel):
    """Body of POST /play/sessions/{id}/seek."""

    position: float = Field(
        ge=0.0,
        description="Target playhead in seconds from the start of the dataset "
        "(clamped to the dataset duration).",
    )
