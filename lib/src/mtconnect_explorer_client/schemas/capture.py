"""Capture-scope models (docs/api.md §7)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .common import SessionInfo


class CaptureCreate(BaseModel):
    """Body of POST /capture/sessions. Defaults mirror the app's client classes."""

    base_url: str = Field(
        min_length=1,
        description="Base URL of the MTConnect agent to capture from.",
    )
    mode: Literal["stream", "sample", "current"] = Field(
        "stream", description="Data collection mode."
    )
    interval_ms: int = Field(
        1000, ge=1, description="Milliseconds between agent stream buffer checks."
    )
    timeout_s: float = Field(
        30.0, gt=0, description="Socket timeout in seconds for non-streaming requests."
    )
    chunk_size: int = Field(
        4096, ge=1, description="Read chunk size in bytes for streaming responses."
    )
    filename: Optional[str] = Field(
        None,
        description="Dataset filename to write. null = server generates one "
        "(sanitized base_url + UTC timestamp, collision-checked).",
    )


class CaptureSessionInfo(SessionInfo):
    """Capture session envelope with a typed config echo."""

    config: CaptureCreate
