"""Shared session/error/port models (docs/api.md §3–§5)."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# Session lifecycle: starting -> running -> (stopping ->) finished | error.
SessionStatus = Literal["starting", "running", "stopping", "finished", "error"]

Scope = Literal["capture", "play", "simulate"]

TERMINAL_STATUSES = ("finished", "error")


class ErrorDetail(BaseModel):
    """Typed error object carried inside FastAPI's native ``detail`` field."""

    code: str = Field(description="Machine-readable error code, e.g. 'port_conflict'.")
    message: str = Field(description="Human-readable description of the error.")
    context: Optional[Dict[str, Any]] = Field(
        None, description="Optional structured context, e.g. the full port-conflict list."
    )


class SessionPorts(BaseModel):
    """Ports held by a session (free once the session reaches a terminal state)."""

    agent: Optional[int] = Field(
        None, description="MTConnect agent HTTP port, or null (capture runs no agent)."
    )
    shdr: List[int] = Field(
        default_factory=list, description="SHDR adapter TCP ports, one per adapter."
    )


class SessionInfo(BaseModel):
    """Common session envelope shared by all three scopes (docs/api.md §5)."""

    id: str = Field(description="Opaque session id, prefixed by scope: cap-1, play-2, sim-3.")
    scope: Scope = Field(description="Owning service: capture, play, or simulate.")
    label: str = Field(description="Human-readable label used to tag log lines.")
    status: SessionStatus = Field(description="Current lifecycle status.")
    created_at: datetime = Field(description="When the session was created (UTC).")
    started_at: Optional[datetime] = Field(
        None, description="When the session reached 'running', or null."
    )
    finished_at: Optional[datetime] = Field(
        None, description="When the session reached a terminal status, or null."
    )
    error: Optional[str] = Field(
        None, description="Error message when status is 'error', else null."
    )
    ports: SessionPorts = Field(
        default_factory=SessionPorts, description="Ports held by this session."
    )
    config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Echo of the scope-specific create config (typed in scope subclasses).",
    )


class PortSuggestions(BaseModel):
    agent: int = Field(description="Suggested free agent HTTP port.")
    shdr: List[int] = Field(description="Suggested free SHDR ports (one per requested count).")


class PortsInfo(BaseModel):
    """Used ports across all live sessions plus free suggestions (docs/api.md §4)."""

    used: List[int] = Field(description="Ports held by every non-terminal session.")
    suggested: PortSuggestions = Field(description="Free ports for pre-filling launch forms.")
