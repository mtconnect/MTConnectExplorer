"""Dataset models (docs/api.md §6)."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class DatasetInfo(BaseModel):
    """One dataset file in the datasets directory."""

    name: str = Field(description="Dataset filename (always ends in .jsonl).")
    size_bytes: int = Field(description="File size in bytes.")
    modified: datetime = Field(description="Last-modified time (UTC).")
    in_use: List[Literal["play", "capture"]] = Field(
        default_factory=list,
        description="Scopes currently using this dataset; empty when idle.",
    )


class RenameRequest(BaseModel):
    """Body of PATCH /datasets/{name}."""

    new_name: str = Field(min_length=1, description="New dataset filename (.jsonl).")


class DeviceInfo(BaseModel):
    """One device from a dataset's probe frame."""

    id: Optional[str] = Field(None, description="Device element id, if present.")
    name: str = Field(description="Device name.")
    uuid: Optional[str] = Field(None, description="Device uuid, if present.")


class DatasetDevices(BaseModel):
    """GET /datasets/{name}/devices — all devices, including the Agent device."""

    devices: List[DeviceInfo] = Field(description="Devices from the probe frame.")
