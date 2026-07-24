"""Play-scope models (docs/api.md §7)."""

from typing import List

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


class PlaySessionInfo(SessionInfo):
    """Play session envelope with a typed config echo."""

    config: PlayCreate
