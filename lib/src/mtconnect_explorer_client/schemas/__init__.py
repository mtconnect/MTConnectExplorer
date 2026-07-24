"""Pydantic request/response models for the MTConnectExplorer API (docs/api.md §9).

This package is the single source of truth for the wire contract: the server's
services layer, the FastAPI routes, and the client all import these classes.
"""

from .common import (
    TERMINAL_STATUSES,
    ErrorDetail,
    PortsInfo,
    PortSuggestions,
    Scope,
    SessionInfo,
    SessionPorts,
    SessionStatus,
)
from .capture import CaptureCreate, CaptureSessionInfo
from .play import AgentSettings, PlayCreate, PlayDevice, PlaySessionInfo
from .simulate import (
    CREATE_MODELS,
    EquipmentInfo,
    LinearAxisLimits,
    Mill3AxisCreate,
    Mill5AxisTrunnionBcCreate,
    Robot6DofCreate,
    RotaryAxisLimits,
    RoutineInfo,
    ShdrSettings,
    SimulateCreate,
    SimulateSessionBase,
    SimulateSessionInfo,
)
from .datasets import DatasetDevices, DatasetInfo, DeviceInfo, RenameRequest
from .events import Event, EventPage, LogEvent, PingEvent, SessionEvent
from .system import AgentInfo, HealthInfo, VersionInfo

__all__ = [
    "TERMINAL_STATUSES",
    "ErrorDetail",
    "PortsInfo",
    "PortSuggestions",
    "Scope",
    "SessionInfo",
    "SessionPorts",
    "SessionStatus",
    "CaptureCreate",
    "CaptureSessionInfo",
    "AgentSettings",
    "PlayCreate",
    "PlayDevice",
    "PlaySessionInfo",
    "CREATE_MODELS",
    "EquipmentInfo",
    "LinearAxisLimits",
    "Mill3AxisCreate",
    "Mill5AxisTrunnionBcCreate",
    "Robot6DofCreate",
    "RotaryAxisLimits",
    "RoutineInfo",
    "ShdrSettings",
    "SimulateCreate",
    "SimulateSessionBase",
    "SimulateSessionInfo",
    "DatasetDevices",
    "DatasetInfo",
    "DeviceInfo",
    "RenameRequest",
    "Event",
    "EventPage",
    "LogEvent",
    "PingEvent",
    "SessionEvent",
    "AgentInfo",
    "HealthInfo",
    "VersionInfo",
]
