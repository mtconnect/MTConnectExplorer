"""System models (docs/api.md §4)."""

from typing import List

from pydantic import BaseModel, Field


class HealthInfo(BaseModel):
    """GET /health — the only unauthenticated endpoint."""

    status: str = Field("ok", description="Liveness indicator.")


class VersionInfo(BaseModel):
    """GET /version."""

    version: str = Field(description="Application version, e.g. 0.1.0-alpha.5.")
    api: str = Field("v1", description="API version.")


class AgentInfo(BaseModel):
    """One locally-running MTConnect agent (run by a live Play/Simulate session)."""

    base_url: str = Field(description="Agent base URL, e.g. http://localhost:5000/.")
    sessions: List[str] = Field(description="Session ids running this agent.")
