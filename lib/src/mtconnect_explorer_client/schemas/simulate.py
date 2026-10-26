"""Simulate-scope models (docs/api.md §7).

Each equipment type has its own create model and its own endpoint
(``POST /simulate/start/{equipment_type}``), so every parameter set is fully
validated by its dedicated schema. Axis limits are **meters** for linear axes
and **degrees** for rotary axes; each limit defaults to the equipment's native
travel, so an empty body ``{}`` reproduces the canned routine identically.
Configured limits scale the routine (and the 3D scene, and the published
MTConnect Constraints) into the given range.
"""

from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Type, Union

from pydantic import BaseModel, Field, model_validator

from .common import SessionInfo
from .play import AgentSettings


class ShdrSettings(BaseModel):
    """SHDR adapter bind settings."""

    host: str = Field("127.0.0.1", description="SHDR adapter host address.")
    port: int = Field(7878, ge=1, le=65535, description="SHDR adapter TCP port.")


class LinearAxisLimits(BaseModel):
    """Travel limits of a linear axis, in meters."""

    min: float = Field(0.0, description="Lower travel limit in meters.")
    max: float = Field(1.0, description="Upper travel limit in meters.")

    @model_validator(mode="after")
    def _ordered(self) -> "LinearAxisLimits":
        if not self.max > self.min:
            raise ValueError(f"max ({self.max}) must be greater than min ({self.min})")
        return self


class RotaryAxisLimits(BaseModel):
    """Rotation limits of a rotary axis or joint, in degrees."""

    min: float = Field(-180.0, description="Lower rotation limit in degrees.")
    max: float = Field(180.0, description="Upper rotation limit in degrees.")

    @model_validator(mode="after")
    def _ordered(self) -> "RotaryAxisLimits":
        if not self.max > self.min:
            raise ValueError(f"max ({self.max}) must be greater than min ({self.min})")
        return self


class SimulateSessionBase(BaseModel):
    """Fields common to every simulate create body."""

    interval_ms: int = Field(
        10, ge=1, le=60000,
        description="Interval in milliseconds at which generated observations "
        "are fed to the SHDR adapter. Lower = smoother motion, higher data rate.",
    )
    agent: AgentSettings = Field(
        default_factory=AgentSettings, description="Bundled MTConnect agent settings."
    )
    shdr: ShdrSettings = Field(
        default_factory=ShdrSettings, description="SHDR adapter bind settings."
    )


class Mill3AxisCreate(SimulateSessionBase):
    """Body of POST /simulate/start/mill_3axis — a 3-axis vertical mill.

    Axis limit defaults are the machine's native travel (0–1 m per axis)."""

    equipment_type: Literal["mill_3axis"] = Field(
        "mill_3axis",
        description="Equipment type (constant; determined by the endpoint).",
    )
    device_name: str = Field(
        "Mill", min_length=1, description="MTConnect device name."
    )
    routine: Literal["Default Routine"] = Field(
        "Default Routine",
        description="Routine to run: a canned facing raster + circular boring cycle.",
    )
    x: LinearAxisLimits = Field(
        default=LinearAxisLimits(min=0.0, max=1.0),
        description="X-axis travel limits in meters.",
    )
    y: LinearAxisLimits = Field(
        default=LinearAxisLimits(min=0.0, max=1.0),
        description="Y-axis travel limits in meters.",
    )
    z: LinearAxisLimits = Field(
        default=LinearAxisLimits(min=0.0, max=1.0),
        description="Z-axis travel limits in meters.",
    )


class Mill5AxisTrunnionBcCreate(SimulateSessionBase):
    """Body of POST /simulate/start/mill_5axis_trunnion_bc — a 5-axis trunnion
    mill: the head carries X/Y/Z; the table tilts about Y (B) and rotates about
    Z (C). C is continuous and therefore has no limits.

    Axis limit defaults are the machine's native travel (0–1 m linear, ±120° B)."""

    equipment_type: Literal["mill_5axis_trunnion_bc"] = Field(
        "mill_5axis_trunnion_bc",
        description="Equipment type (constant; determined by the endpoint).",
    )
    device_name: str = Field(
        "TrunnionMill", min_length=1, description="MTConnect device name."
    )
    routine: Literal["Default Routine"] = Field(
        "Default Routine",
        description="Routine to run: canned 3+2 indexed facing passes followed "
        "by a simultaneous 5-axis helical contour.",
    )
    x: LinearAxisLimits = Field(
        default=LinearAxisLimits(min=0.0, max=1.0),
        description="X-axis travel limits in meters.",
    )
    y: LinearAxisLimits = Field(
        default=LinearAxisLimits(min=0.0, max=1.0),
        description="Y-axis travel limits in meters.",
    )
    z: LinearAxisLimits = Field(
        default=LinearAxisLimits(min=0.0, max=1.0),
        description="Z-axis travel limits in meters.",
    )
    b: RotaryAxisLimits = Field(
        default=RotaryAxisLimits(min=-120.0, max=120.0),
        description="Trunnion tilt about Y (B axis) limits in degrees.",
    )


class Robot6DofCreate(SimulateSessionBase):
    """Body of POST /simulate/start/robot_6dof — a generic 6-DOF articulated
    robot running a pick-and-place cycle in joint space.

    Joint limit defaults are the arm's native travel; link lengths are fixed
    physical structure."""

    equipment_type: Literal["robot_6dof"] = Field(
        "robot_6dof",
        description="Equipment type (constant; determined by the endpoint).",
    )
    device_name: str = Field(
        "Robot", min_length=1, description="MTConnect device name."
    )
    routine: Literal["Default Routine"] = Field(
        "Default Routine",
        description="Routine to run: a canned pick-and-place cycle.",
    )
    j1: RotaryAxisLimits = Field(
        default=RotaryAxisLimits(min=-170.0, max=170.0),
        description="J1 (base yaw, about Z) limits in degrees.",
    )
    j2: RotaryAxisLimits = Field(
        default=RotaryAxisLimits(min=-100.0, max=135.0),
        description="J2 (shoulder pitch, about Y) limits in degrees.",
    )
    j3: RotaryAxisLimits = Field(
        default=RotaryAxisLimits(min=-80.0, max=155.0),
        description="J3 (elbow pitch, about Y) limits in degrees.",
    )
    j4: RotaryAxisLimits = Field(
        default=RotaryAxisLimits(min=-180.0, max=180.0),
        description="J4 (forearm roll, about Z) limits in degrees.",
    )
    j5: RotaryAxisLimits = Field(
        default=RotaryAxisLimits(min=-120.0, max=120.0),
        description="J5 (wrist pitch, about Y) limits in degrees.",
    )
    j6: RotaryAxisLimits = Field(
        default=RotaryAxisLimits(min=-180.0, max=180.0),
        description="J6 (wrist roll, about Z) limits in degrees.",
    )


# The discriminated union of every create body — the session envelope's typed
# config echo, and the type the client's simulate_start() dispatches on.
SimulateCreate = Annotated[
    Union[Mill3AxisCreate, Mill5AxisTrunnionBcCreate, Robot6DofCreate],
    Field(discriminator="equipment_type"),
]

# equipment_type key -> create model (form scaffolding, client dispatch).
CREATE_MODELS: Dict[str, Type[SimulateSessionBase]] = {
    "mill_3axis": Mill3AxisCreate,
    "mill_5axis_trunnion_bc": Mill5AxisTrunnionBcCreate,
    "robot_6dof": Robot6DofCreate,
}


class EquipmentInfo(BaseModel):
    """One entry of the GET /simulate/equipment catalog."""

    type: str = Field(description="Equipment type key, e.g. mill_3axis.")
    label: str = Field(description="Human-readable equipment label.")
    default_device_name: str = Field(description="Default MTConnect device name.")
    routines: List[str] = Field(description="Available routine labels.")
    create_path: str = Field(
        description="Endpoint that starts a session of this type, e.g. "
        "/api/v1/simulate/start/mill_3axis; its request schema is that "
        "operation's body in the OpenAPI document."
    )


class RoutineInfo(BaseModel):
    """GET /simulate/sessions/{id}/routine — feeds the phase-locked 3D view."""

    spec: Dict[str, Any] = Field(
        description="RoutineSpec.to_dict(): equipment, coord_names, start, cycle_time, "
        "geometry, moves — scaled into the session's configured axis limits."
    )
    start_epoch: Optional[float] = Field(
        None,
        description="Unix time emission began; null until the session reaches 'running'.",
    )
    server_time: datetime = Field(
        description="Server clock at response time, for client clock-offset correction."
    )


class SimulateSessionInfo(SessionInfo):
    """Simulate session envelope with a typed config echo."""

    config: SimulateCreate
