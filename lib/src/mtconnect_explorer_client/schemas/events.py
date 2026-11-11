"""Event-bus models (docs/api.md §8).

Every line/event carries a globally monotonic ``seq`` and an RFC 3339 ``ts``.
Pings are cursor checkpoints: they carry the last delivered event's seq and are
never stored in the ring buffer. Clients must ignore unknown event types and
unknown fields (forward compatibility).
"""

from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from .common import Scope, SessionStatus


class LogEvent(BaseModel):
    """A backend log record, attributed to a session when one emitted it."""

    type: Literal["log"] = "log"
    seq: int = Field(description="Globally monotonic sequence number.")
    ts: datetime = Field(description="Event timestamp (UTC).")
    scope: Optional[Scope] = Field(None, description="Owning scope, if session-attributed.")
    session: Optional[str] = Field(None, description="Session id, if session-attributed.")
    level: str = Field(description="Log level name, e.g. INFO.")
    logger: str = Field(description="Logger name, e.g. src.converter.")
    msg: str = Field(description="Log message.")


class SessionEvent(BaseModel):
    """A session lifecycle transition (posted on every status change)."""

    type: Literal["session"] = "session"
    seq: int = Field(description="Globally monotonic sequence number.")
    ts: datetime = Field(description="Event timestamp (UTC).")
    scope: Scope = Field(description="Owning scope.")
    session: str = Field(description="Session id.")
    event: str = Field("status", description="Event kind (currently always 'status').")
    status: SessionStatus = Field(description="New session status.")
    detail: Optional[Dict[str, Any]] = Field(
        None, description="Extra transition data, e.g. {'start_epoch': ...} on simulate."
    )


class PingEvent(BaseModel):
    """Stream heartbeat; ``seq`` is the last delivered event's seq (a checkpoint)."""

    type: Literal["ping"] = "ping"
    seq: int = Field(description="Last delivered event's seq — 'current through N'.")
    ts: datetime = Field(description="Ping timestamp (UTC).")


Event = Annotated[Union[LogEvent, SessionEvent, PingEvent], Field(discriminator="type")]


class EventPage(BaseModel):
    """GET /events poll response."""

    events: List[Event] = Field(description="Events after the cursor, oldest first.")
    next: int = Field(description="Cursor for the next poll (pass as ?after=).")
    dropped: bool = Field(
        description="True if the cursor fell off the ring buffer's tail (gap)."
    )
