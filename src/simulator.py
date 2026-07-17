"""
Equipment simulator — the "data source" for the Simulator page.

Where the Play page replays a recorded dataset through the SHDR adapter, this
module *generates* live synthetic data for two virtual machines and streams it
through the same pipeline (``ShdrAdapter`` -> bundled ``agent.exe`` -> HTTP):

    generated <Device> XML + routine values -> ShdrAdapter -> Agent -> HTTP

Two pieces of equipment are supported:

  * ``MILL_3AXIS``  — a 3-axis vertical mill (X/Y/Z, each 0..1000 mm) running a
    canned program: trace the stock bounding box, a facing raster, then a
    circular boring pattern (stepping deeper in Z). Loops.
  * ``ROBOT_6DOF``  — a generic 6-DOF articulated robot running a pick-and-place
    cycle defined in joint space. Loops.

A routine is expressed as an ordered list of piecewise-linear *moves*
(``_Move``). The same routine description (``RoutineSpec.to_dict()``) is handed
to the 3D visualization (``src/viz.py``), which re-runs the identical piecewise
interpolation in JavaScript so the on-screen motion matches the emitted data.

The device model is delivered to the agent over the SHDR ``* deviceModel:``
handshake (see ``src/shdr.py``); it only needs to be a ``<Device>`` fragment,
not an XSD-validated devices file (the agent runs with
``EnableSourceDeviceModels = true``).
"""

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from .shdr import ShdrAdapter, ShdrAdapterConfiguration, ShdrDataItem

logger = logging.getLogger(__name__)


# ===========================================================================
# Equipment types
# ===========================================================================
class EquipmentType(str, Enum):
    MILL_3AXIS = "MILL_3AXIS"
    ROBOT_6DOF = "ROBOT_6DOF"

    @property
    def label(self) -> str:
        return {
            EquipmentType.MILL_3AXIS: "3-Axis Mill",
            EquipmentType.ROBOT_6DOF: "6-DOF Robot",
        }[self]

    @property
    def default_device_name(self) -> str:
        return {
            EquipmentType.MILL_3AXIS: "Mill",
            EquipmentType.ROBOT_6DOF: "Robot",
        }[self]

    @property
    def routine_name(self) -> str:
        """Human label for the (single, for now) canned routine of this type."""
        return {
            EquipmentType.MILL_3AXIS: "Canned G-Code Routine",
            EquipmentType.ROBOT_6DOF: "Canned Pick-and-Place",
        }[self]


# ===========================================================================
# Routine representation
#
# A routine is a list of piecewise-linear segments (_Move). Each move
# interpolates a vector of continuous coordinates (mill: [x, y, z]; robot:
# [j1..j6]) from the previous move's target to this move's target over
# ``duration`` seconds, while holding a set of discrete/meta values
# (program, execution state, gripper, feed, spindle, ...).
# ===========================================================================
@dataclass
class _Move:
    target: Tuple[float, ...]
    duration: float
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutineSpec:
    """A fully-described, periodic routine shared by the emitter and the viz."""

    equipment: EquipmentType
    coord_names: Tuple[str, ...]          # names of the continuous coordinates
    start: Tuple[float, ...]              # coordinate vector at phase 0
    moves: List[_Move]
    geometry: Dict[str, Any]              # machine dimensions / link lengths (viz)

    @property
    def cycle_time(self) -> float:
        return sum(m.duration for m in self.moves) or 1.0

    def _segment(self, phase: float) -> Tuple[Tuple[float, ...], Tuple[float, ...], float, Dict[str, Any]]:
        """Return (from_vec, to_vec, local_t, meta) for the given cycle phase."""
        phase = phase % self.cycle_time
        prev = self.start
        acc = 0.0
        for mv in self.moves:
            if phase < acc + mv.duration or mv is self.moves[-1]:
                local = 0.0 if mv.duration <= 0 else (phase - acc) / mv.duration
                local = min(max(local, 0.0), 1.0)
                return prev, mv.target, local, mv.meta
            acc += mv.duration
            prev = mv.target
        # Unreachable (loop always returns on the last move), but keep types sane.
        return prev, prev, 0.0, {}

    def coords(self, phase: float) -> Tuple[List[float], List[float], Dict[str, Any]]:
        """Interpolated coordinate vector + segment velocity (per second) + meta."""
        a, b, t, meta = self._segment(phase)
        pos = [a[i] + (b[i] - a[i]) * t for i in range(len(a))]
        dur = max(meta.get("_dur", 1e-6), 1e-6)
        vel = [(b[i] - a[i]) / dur for i in range(len(a))]
        return pos, vel, meta

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable form consumed by the 3D visualization."""
        return {
            "equipment": self.equipment.value,
            "coord_names": list(self.coord_names),
            "start": list(self.start),
            "cycle_time": self.cycle_time,
            "geometry": self.geometry,
            "moves": [{"target": list(m.target), "duration": m.duration, "meta": m.meta} for m in self.moves],
        }


# ---------------------------------------------------------------------------
# Robot forward kinematics (a generic 6-DOF articulated arm)
#
# A 6-link serial chain. Each joint applies, in the parent frame, a translation
# to the joint origin followed by a rotation about the joint axis. This is the
# SAME order used to nest THREE.Group objects in the viz, so the Python TCP and
# the on-screen tool tip agree. Reach ~665 mm.
# ---------------------------------------------------------------------------
# offset from previous joint origin (mm), rotation axis for the joint angle.
ROBOT_LINKS: List[Dict[str, Any]] = [
    {"offset": (0.0, 0.0, 0.0), "axis": "z"},   # J1 base yaw
    {"offset": (0.0, 0.0, 120.0), "axis": "y"}, # J2 shoulder pitch
    {"offset": (0.0, 0.0, 200.0), "axis": "y"}, # J3 elbow pitch
    {"offset": (0.0, 0.0, 170.0), "axis": "z"}, # J4 forearm roll
    {"offset": (0.0, 0.0, 35.0), "axis": "y"},  # J5 wrist pitch
    {"offset": (0.0, 0.0, 75.0), "axis": "z"},  # J6 wrist roll
]
ROBOT_TOOL_OFFSET = (0.0, 0.0, 65.0)            # flange -> TCP


def _rot_matrix(axis: str, deg: float) -> List[List[float]]:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    if axis == "x":
        return [[1, 0, 0], [0, c, -s], [0, s, c]]
    if axis == "y":
        return [[c, 0, s], [0, 1, 0], [-s, 0, c]]
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]   # z


def _mat_mul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _mat_vec(a: List[List[float]], v: Tuple[float, float, float]) -> Tuple[float, float, float]:
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))  # type: ignore[return-value]


def robot_fk(angles_deg: List[float]) -> Tuple[float, float, float]:
    """Tool-center-point (mm) for the six joint angles (degrees)."""
    rot = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]  # identity
    pos = (0.0, 0.0, 0.0)
    for i, link in enumerate(ROBOT_LINKS):
        # translate to this joint's origin (in current frame), then rotate.
        off = _mat_vec(rot, link["offset"])
        pos = (pos[0] + off[0], pos[1] + off[1], pos[2] + off[2])
        rot = _mat_mul(rot, _rot_matrix(link["axis"], angles_deg[i]))
    tip = _mat_vec(rot, ROBOT_TOOL_OFFSET)
    return (pos[0] + tip[0], pos[1] + tip[1], pos[2] + tip[2])


# ===========================================================================
# Device model builders (return <Device> XML fragments)
# ===========================================================================
def build_mill_device_model(device_name: str, uuid: str) -> str:
    return dedent(f"""
    <Device uuid="{uuid}" name="{device_name}" id="{uuid}">
      <Description model="3-Axis VMC" manufacturer="Generic">Simulated 3-axis vertical machining center</Description>
      <DataItems>
        <DataItem id="avail" category="EVENT" type="AVAILABILITY"   name="avail"/>
        <DataItem id="estop" category="EVENT" type="EMERGENCY_STOP" name="estop"/>
      </DataItems>
      <Components>
        <Axes id="axes" name="Axes">
          <Components>
            <Linear id="x" name="X">
              <DataItems>
                <DataItem id="Xact"  category="SAMPLE" type="POSITION" subType="ACTUAL" name="Xact"  units="MILLIMETER" nativeUnits="MILLIMETER"/>
                <DataItem id="Xload" category="SAMPLE" type="LOAD"                      name="Xload" units="PERCENT"    nativeUnits="PERCENT"/>
              </DataItems>
            </Linear>
            <Linear id="y" name="Y">
              <DataItems>
                <DataItem id="Yact"  category="SAMPLE" type="POSITION" subType="ACTUAL" name="Yact"  units="MILLIMETER" nativeUnits="MILLIMETER"/>
                <DataItem id="Yload" category="SAMPLE" type="LOAD"                      name="Yload" units="PERCENT"    nativeUnits="PERCENT"/>
              </DataItems>
            </Linear>
            <Linear id="z" name="Z">
              <DataItems>
                <DataItem id="Zact"  category="SAMPLE" type="POSITION" subType="ACTUAL" name="Zact"  units="MILLIMETER" nativeUnits="MILLIMETER"/>
                <DataItem id="Zload" category="SAMPLE" type="LOAD"                      name="Zload" units="PERCENT"    nativeUnits="PERCENT"/>
              </DataItems>
            </Linear>
            <Rotary id="c1" name="C1">
              <DataItems>
                <DataItem id="Sspeed" category="SAMPLE" type="ROTARY_VELOCITY" subType="ACTUAL" name="Sspeed" units="REVOLUTION/MINUTE" nativeUnits="REVOLUTION/MINUTE"/>
                <DataItem id="Sload"  category="SAMPLE" type="LOAD"                             name="Sload"  units="PERCENT"           nativeUnits="PERCENT"/>
                <DataItem id="Smode"  category="EVENT"  type="ROTARY_MODE"                      name="Smode">
                  <Constraints><Value>SPINDLE</Value></Constraints>
                </DataItem>
              </DataItems>
            </Rotary>
          </Components>
        </Axes>
        <Controller id="ctrl" name="Controller">
          <DataItems>
            <DataItem id="cmode" category="EVENT" type="CONTROLLER_MODE" name="cmode"/>
            <DataItem id="exec"  category="EVENT" type="EXECUTION"       name="exec"/>
            <DataItem id="prog"  category="EVENT" type="PROGRAM" name="prog"/>
          </DataItems>
          <Components>
            <Path id="path" name="path">
              <DataItems>
                <DataItem id="Fact"    category="SAMPLE" type="PATH_FEEDRATE" subType="ACTUAL" name="Fact"    units="MILLIMETER/SECOND" nativeUnits="MILLIMETER/SECOND"/>
                <DataItem id="pathpos" category="SAMPLE" type="PATH_POSITION" subType="ACTUAL" name="pathpos" units="MILLIMETER_3D"      nativeUnits="MILLIMETER_3D"/>
              </DataItems>
            </Path>
          </Components>
        </Controller>
      </Components>
    </Device>
    """).strip()


def build_robot_device_model(device_name: str, uuid: str) -> str:
    # Articulated-robot joints are modeled as Rotary axes named J1..Jn (not the
    # A/B/C of a machine tool), each reporting its angular POSITION via an ANGLE
    # SAMPLE — per the MTConnect standard's robot modeling guidance.
    joints = ""
    for i in range(1, 7):
        joints += dedent(f"""
            <Rotary id="joint{i}" name="J{i}">
              <DataItems>
                <DataItem id="J{i}" category="SAMPLE" type="ANGLE" subType="ACTUAL" name="J{i}" units="DEGREE" nativeUnits="DEGREE"/>
              </DataItems>
            </Rotary>
        """).rstrip("\n")
    return dedent(f"""
    <Device uuid="{uuid}" name="{device_name}" id="{uuid}">
      <Description model="6-Axis Articulated Robot" manufacturer="Generic">Simulated 6-DOF articulated robot</Description>
      <DataItems>
        <DataItem id="avail" category="EVENT" type="AVAILABILITY"   name="avail"/>
        <DataItem id="estop" category="EVENT" type="EMERGENCY_STOP" name="estop"/>
      </DataItems>
      <Components>
        <Axes id="axes" name="Axes">
          <Components>{joints}
          </Components>
        </Axes>
        <Controller id="ctrl" name="Controller">
          <DataItems>
            <DataItem id="cmode" category="EVENT" type="CONTROLLER_MODE" name="cmode"/>
            <DataItem id="exec"  category="EVENT" type="EXECUTION"       name="exec"/>
            <DataItem id="prog"  category="EVENT" type="PROGRAM" name="prog"/>
          </DataItems>
          <Components>
            <Path id="path" name="path">
              <DataItems>
                <DataItem id="Fact"    category="SAMPLE" type="PATH_FEEDRATE" subType="ACTUAL" name="Fact"    units="MILLIMETER/SECOND" nativeUnits="MILLIMETER/SECOND"/>
                <DataItem id="pathpos" category="SAMPLE" type="PATH_POSITION" subType="ACTUAL" name="pathpos" units="MILLIMETER_3D"      nativeUnits="MILLIMETER_3D"/>
              </DataItems>
            </Path>
          </Components>
        </Controller>
        <EndEffector id="effector" name="EndEffector">
          <Components>
            <Gripper id="gripper" name="Gripper">
              <DataItems>
                <!-- MTConnect 2.x has EndEffector/Gripper components but no dedicated
                     gripper-state type; CHUCK_STATE (OPEN/CLOSED) is the standard
                     open/closed holding-state used for the grasping mechanism. -->
                <DataItem id="grip" category="EVENT" type="CHUCK_STATE" name="grip"/>
              </DataItems>
            </Gripper>
          </Components>
        </EndEffector>
      </Components>
    </Device>
    """).strip()


# ===========================================================================
# Routine builders
# ===========================================================================
# Mill work volume (mm). X/Y span 0..1000 with a centered block. Z sits the
# stock on the table near the machine base (the floor grid), so the block rests
# on the table rather than floating high in the envelope. The stock is 150 mm
# thick; the tool retracts to a safe plane just above it.
_MILL_X0, _MILL_X1 = 300.0, 700.0
_MILL_Y0, _MILL_Y1 = 300.0, 700.0
_MILL_Z_BOT = 40.0             # stock bottom (rests on the table plate at the base)
_MILL_Z_TOP = 190.0            # stock top surface (150 mm-thick block)
_MILL_Z_SAFE = 240.0           # rapid/retract plane (50 mm above the stock)
_MILL_RAPID = 400.0            # mm/s
_MILL_FEED = 130.0             # mm/s (compressed from a realistic ~60 so one full
                               # program loop is ~2 min — comfortable to watch)
_MILL_TOOL_DIA = 60.0          # face/end-mill diameter (mm)
_MILL_STEPOVER = 0.9 * _MILL_TOOL_DIA   # ~90% of the tool width (removing material)
_MILL_DOC = 12.0               # depth of cut per facing pass (mm)
_MILL_FACE_LEVELS = 4          # progressive facing passes, working down into the part


def _seg(moves: List[_Move], target: Tuple[float, float, float], speed: float, meta: Dict[str, Any],
         prev: List[float]) -> None:
    """Append a linear move to ``target`` at ``speed`` (mm/s), timing it by distance."""
    dist = math.dist(prev, target)
    dur = max(dist / speed, 0.05)
    m = dict(meta)
    m["_dur"] = dur
    m["feed"] = speed
    moves.append(_Move(target=target, duration=dur, meta=m))
    prev[:] = list(target)


def build_mill_routine() -> RoutineSpec:
    moves: List[_Move] = []
    start = (_MILL_X0, _MILL_Y0, _MILL_Z_SAFE)
    prev = list(start)

    rapid = {"exec": "ACTIVE", "prog": "FACING.NC", "spindle": 0.0, "cutting": False}
    cut_face = {"exec": "ACTIVE", "prog": "FACING.NC", "spindle": 3000.0, "cutting": True}
    cut_bore = {"exec": "ACTIVE", "prog": "BORING.NC", "spindle": 3500.0, "cutting": True}

    # X columns spaced by ~90% of the tool width (so passes overlap like real
    # facing removing material).
    xs: List[float] = []
    x = _MILL_X0
    while x <= _MILL_X1 + 1e-6:
        xs.append(x)
        x += _MILL_STEPOVER
    if xs[-1] < _MILL_X1 - 1e-6:
        xs.append(_MILL_X1)

    # ---- Facing: start at the top of the part and work down into it ----
    # Each pass is a full Y raster stepping over in X; successive passes cut a
    # depth-of-cut deeper, so the tool progressively faces down into the stock.
    for level in range(_MILL_FACE_LEVELS):
        zlevel = _MILL_Z_TOP - (level + 1) * _MILL_DOC
        _seg(moves, (xs[0], _MILL_Y0, _MILL_Z_SAFE), _MILL_RAPID, rapid, prev)
        _seg(moves, (xs[0], _MILL_Y0, zlevel), _MILL_FEED, cut_face, prev)
        for i, xv in enumerate(xs):
            y_far = _MILL_Y1 if i % 2 == 0 else _MILL_Y0
            _seg(moves, (xv, y_far, zlevel), _MILL_FEED, cut_face, prev)   # traverse Y
            if i < len(xs) - 1:
                _seg(moves, (xs[i + 1], y_far, zlevel), _MILL_FEED, cut_face, prev)  # step over X
        _seg(moves, (prev[0], prev[1], _MILL_Z_SAFE), _MILL_RAPID, rapid, prev)

    # ---- Circular boring, helical step-down ----
    cx, cy, r = 500.0, 500.0, 90.0
    z_faced = _MILL_Z_TOP - _MILL_FACE_LEVELS * _MILL_DOC   # bore from the faced surface
    _seg(moves, (cx + r, cy, _MILL_Z_SAFE), _MILL_RAPID, {**rapid, "prog": "BORING.NC"}, prev)
    _seg(moves, (cx + r, cy, z_faced), _MILL_FEED, cut_bore, prev)
    revs, seg_per_rev = 3, 24
    z_start, z_end = z_faced, z_faced - 60.0
    total = revs * seg_per_rev
    for k in range(1, total + 1):
        ang = 2.0 * math.pi * k / seg_per_rev
        zc = z_start + (z_end - z_start) * (k / total)
        _seg(moves, (cx + r * math.cos(ang), cy + r * math.sin(ang), zc), _MILL_FEED, cut_bore, prev)
    for k in range(1, seg_per_rev + 1):        # one clean finishing pass at depth
        ang = 2.0 * math.pi * k / seg_per_rev
        _seg(moves, (cx + r * math.cos(ang), cy + r * math.sin(ang), z_end), _MILL_FEED, cut_bore, prev)
    _seg(moves, (cx + r, cy, _MILL_Z_SAFE), _MILL_RAPID, {**rapid, "prog": "BORING.NC"}, prev)
    _seg(moves, list(start), _MILL_RAPID, rapid, prev)

    geometry = {
        "limit": 1000.0,
        "tool_dia": _MILL_TOOL_DIA,
        "work": {"x0": _MILL_X0, "x1": _MILL_X1, "y0": _MILL_Y0, "y1": _MILL_Y1,
                 "z_bot": _MILL_Z_BOT, "z_top": _MILL_Z_TOP, "z_safe": _MILL_Z_SAFE},
    }
    return RoutineSpec(EquipmentType.MILL_3AXIS, ("Xact", "Yact", "Zact"), start, moves, geometry)


# Robot pick-and-place, joint-space waypoints (degrees) [J1..J6].
#
# Every pick/place-column pose keeps the end effector pointing STRAIGHT DOWN by
# holding theta4 = theta6 = 0 and theta2 + theta3 + theta5 = 180 deg: the base
# yaw (theta1) only aims the arm horizontally, and the three Y-axis pitches then
# sum to a 180 deg tool tilt (local +Z -> world -Z). Pick/place are on opposite
# sides (theta1 = +/-60) at the same reach so the tool descends vertically.
_R_HOME = (0.0, 25.0, 55.0, 0.0, 40.0, 0.0)
_R_OVER_PICK = (60.0, 35.0, 60.0, 0.0, 85.0, 0.0)     # high, tool down (35+60+85=180)
_R_PICK = (60.0, 45.0, 75.0, 0.0, 60.0, 0.0)          # low,  tool down (45+75+60=180)
_R_LIFT_PICK = (60.0, 35.0, 60.0, 0.0, 85.0, 0.0)
_R_OVER_PLACE = (-60.0, 35.0, 60.0, 0.0, 85.0, 0.0)
_R_PLACE = (-60.0, 45.0, 75.0, 0.0, 60.0, 0.0)
_R_LIFT_PLACE = (-60.0, 35.0, 60.0, 0.0, 85.0, 0.0)


def build_robot_routine() -> RoutineSpec:
    moves: List[_Move] = []

    def mv(target, dur, prog, exec_, grip):
        moves.append(_Move(target=tuple(float(v) for v in target), duration=dur,
                           meta={"_dur": dur, "prog": prog, "exec": exec_, "grip": grip}))

    P = "PICK_PLACE.PROG"
    mv(_R_OVER_PICK, 2.5, P, "ACTIVE", "OPEN")
    mv(_R_PICK, 2.0, P, "ACTIVE", "OPEN")
    mv(_R_PICK, 1.0, P, "ACTIVE", "CLOSED")       # dwell: grasp
    mv(_R_LIFT_PICK, 2.0, P, "ACTIVE", "CLOSED")
    mv(_R_OVER_PLACE, 3.0, P, "ACTIVE", "CLOSED")
    mv(_R_PLACE, 2.0, P, "ACTIVE", "CLOSED")
    mv(_R_PLACE, 1.0, P, "ACTIVE", "OPEN")        # dwell: release
    mv(_R_LIFT_PLACE, 2.0, P, "ACTIVE", "OPEN")
    mv(_R_HOME, 3.0, P, "ACTIVE", "OPEN")

    geometry = {
        "links": [{"offset": list(l["offset"]), "axis": l["axis"]} for l in ROBOT_LINKS],
        "tool_offset": list(ROBOT_TOOL_OFFSET),
    }
    return RoutineSpec(EquipmentType.ROBOT_6DOF, ("J1", "J2", "J3", "J4", "J5", "J6"),
                       _R_HOME, moves, geometry)


def build_routine(equipment: EquipmentType) -> RoutineSpec:
    if equipment == EquipmentType.MILL_3AXIS:
        return build_mill_routine()
    return build_robot_routine()


# ===========================================================================
# Value formatting
# ===========================================================================
def _fmt(value: Any) -> str:
    """Serialize a value for SHDR, stripping a trailing ``.0``.

    The bundled agent reads a float sent with a trailing ``.0`` back as an int
    (documented in src/test.py), which breaks loopback type round-tripping.
    """
    s = str(value)
    if s.endswith(".0"):
        s = s[:-2]
    if ".0 " in s:
        s = s.replace(".0 ", " ")
    return s


# ===========================================================================
# The simulator (owns a ShdrAdapter + a routine-driven worker thread)
# ===========================================================================
class EquipmentSimulator:
    """Streams a canned routine for one virtual machine through a SHDR adapter."""

    def __init__(
        self,
        equipment: EquipmentType,
        device_name: str,
        host: str = "127.0.0.1",
        port: int = 7878,
        thread_name: str = "equipment-simulator",
        tick_s: float = 0.01,
    ) -> None:
        self._equipment = equipment
        self._device_name = device_name
        self._thread_name = thread_name
        self._tick_s = tick_s

        self._spec = build_routine(equipment)
        uuid = f"{device_name}.sim"
        if equipment == EquipmentType.MILL_3AXIS:
            device_model = build_mill_device_model(device_name, uuid)
        else:
            device_model = build_robot_device_model(device_name, uuid)

        self._adapter = ShdrAdapter(ShdrAdapterConfiguration(
            device=device_name,
            device_model=device_model,
            host=host,
            port=port,
        ))

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_alive = False
        self._start_epoch: Optional[float] = None
        self._last: Dict[str, Any] = {}

    # -- accessors -----------------------------------------------------------
    @property
    def equipment(self) -> EquipmentType:
        return self._equipment

    @property
    def adapter(self) -> ShdrAdapter:
        return self._adapter

    @property
    def spec(self) -> RoutineSpec:
        return self._spec

    @property
    def is_alive(self) -> bool:
        return self._is_alive

    @property
    def start_epoch(self) -> Optional[float]:
        return self._start_epoch

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "EquipmentSimulator":
        """Start the SHDR adapter (TCP server). Call play() once the agent connects."""
        self._adapter.start()
        return self

    def play(self) -> None:
        if self._thread is not None:
            raise RuntimeError("EquipmentSimulator is already playing")
        self._thread = threading.Thread(target=self._loop, name=self._thread_name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            self._adapter.stop()
        except Exception:
            pass

    # -- worker --------------------------------------------------------------
    def _enqueue_changed(self, name: str, value: Any, ts: datetime) -> None:
        s = _fmt(value)
        if self._last.get(name) != s:
            self._last[name] = s
            self._adapter.enqueue(ShdrDataItem(timestamp=ts, name=name, value=s))

    def _loop(self) -> None:
        try:
            self._is_alive = True
            self._start_epoch = time.time()
            logger.info("simulator (%s) started", self._equipment.label)

            # Static / one-time observations.
            now = datetime.now(timezone.utc)
            for name, value in (("avail", "AVAILABLE"), ("estop", "ARMED"), ("cmode", "AUTOMATIC")):
                self._enqueue_changed(name, value, now)
            if self._equipment == EquipmentType.MILL_3AXIS:
                self._enqueue_changed("Smode", "SPINDLE", now)

            cycle = self._spec.cycle_time
            while not self._stop_event.is_set():
                phase = (time.time() - self._start_epoch) % cycle
                ts = datetime.now(timezone.utc)
                if self._equipment == EquipmentType.MILL_3AXIS:
                    self._emit_mill(phase, ts)
                else:
                    self._emit_robot(phase, ts)
                time.sleep(self._tick_s)

            logger.info("simulator (%s) stopped", self._equipment.label)
        except Exception as ex:  # pragma: no cover - defensive
            logger.error("simulator thread exception: %s", ex)
        finally:
            self._is_alive = False

    def _emit_mill(self, phase: float, ts: datetime) -> None:
        (x, y, z), vel, meta = self._spec.coords(phase)
        self._enqueue_changed("Xact", round(x, 2), ts)
        self._enqueue_changed("Yact", round(y, 2), ts)
        self._enqueue_changed("Zact", round(z, 2), ts)
        cutting = bool(meta.get("cutting"))
        base = 30.0 if cutting else 3.0
        self._enqueue_changed("Xload", round(min(100.0, base + 0.25 * abs(vel[0])), 1), ts)
        self._enqueue_changed("Yload", round(min(100.0, base + 0.25 * abs(vel[1])), 1), ts)
        self._enqueue_changed("Zload", round(min(100.0, base + 0.40 * abs(vel[2])), 1), ts)
        self._enqueue_changed("Sspeed", round(meta.get("spindle", 0.0), 1), ts)
        self._enqueue_changed("Sload", round(45.0 if cutting else 5.0, 1), ts)
        self._enqueue_changed("Fact", round(meta.get("feed", 0.0), 1), ts)
        self._enqueue_changed("pathpos", f"{round(x, 2)} {round(y, 2)} {round(z, 2)}", ts)
        if meta.get("prog"):
            self._enqueue_changed("prog", meta["prog"], ts)
        if meta.get("exec"):
            self._enqueue_changed("exec", meta["exec"], ts)

    def _emit_robot(self, phase: float, ts: datetime) -> None:
        angles, vel, meta = self._spec.coords(phase)
        # Emit each joint's angular POSITION (J1..J6). No load — position is the
        # value of interest for the robot.
        for i in range(6):
            self._enqueue_changed(f"J{i + 1}", round(angles[i], 2), ts)
        tcp = robot_fk(angles)
        self._enqueue_changed("pathpos", f"{round(tcp[0], 1)} {round(tcp[1], 1)} {round(tcp[2], 1)}", ts)
        moving = any(abs(v) > 0.1 for v in vel)
        self._enqueue_changed("Fact", round(120.0 if moving else 0.0, 1), ts)
        if meta.get("prog"):
            self._enqueue_changed("prog", meta["prog"], ts)
        if meta.get("exec"):
            self._enqueue_changed("exec", meta["exec"], ts)
        if meta.get("grip"):
            self._enqueue_changed("grip", meta["grip"], ts)
