"""Locks the axis-limit contract together across its three homes:

1. The lib create models' default limits == the simulator's NATIVE_TRAVEL
   (meters/degrees on the wire vs mm/deg internally), so a default request
   reproduces the canned motion identically.
2. Every canned routine stays inside its native travel, so any configured
   limits contain the scaled motion (the affine map preserves containment).
3. Scaling actually maps the motion into configured limits, and the default
   (identity) case is bit-for-bit the canned routine.
"""

import math

from mtconnect_explorer_client.schemas import (
    CREATE_MODELS,
    LinearAxisLimits,
    RotaryAxisLimits,
)

from src.simulator import COORD_AXES, NATIVE_TRAVEL, EquipmentType, build_routine


def _model_limits_mm(model) -> dict:
    """A create model's default limits, converted to simulator units (mm/deg)."""
    out = {}
    for name, field in model.model_fields.items():
        default = field.default
        if isinstance(default, LinearAxisLimits):
            out[name] = (default.min * 1000.0, default.max * 1000.0)
        elif isinstance(default, RotaryAxisLimits):
            out[name] = (default.min, default.max)
    return out


def test_lib_defaults_match_native_travel():
    for key, model in CREATE_MODELS.items():
        assert _model_limits_mm(model) == NATIVE_TRAVEL[key], (
            f"{key}: the lib schema defaults must mirror simulator.NATIVE_TRAVEL"
        )


def test_canned_routines_stay_inside_native_travel():
    for equipment in EquipmentType:
        spec = build_routine(equipment)
        native = NATIVE_TRAVEL[equipment.value]
        axes = COORD_AXES[equipment.value]
        for move in [type("M", (), {"target": spec.start})] + list(spec.moves):
            for i, axis in enumerate(axes):
                if axis is None:
                    continue  # continuous (unlimited) axis, e.g. the trunnion's C
                lo, hi = native[axis]
                v = move.target[i]
                assert lo - 1e-6 <= v <= hi + 1e-6, (
                    f"{equipment.value}: canned {axis}={v} outside native travel {lo}..{hi}"
                )


def test_default_limits_are_identity():
    for equipment in EquipmentType:
        canned = build_routine(equipment)
        defaulted = build_routine(equipment, dict(NATIVE_TRAVEL[equipment.value]))
        assert defaulted.start == canned.start
        assert [m.target for m in defaulted.moves] == [m.target for m in canned.moves]
        assert [m.duration for m in defaulted.moves] == [m.duration for m in canned.moves]


def test_scaled_routine_respects_configured_limits():
    limits = {"x": (100.0, 500.0), "y": (0.0, 250.0), "z": (50.0, 300.0)}
    spec = build_routine(EquipmentType.MILL_3AXIS, limits)
    for i, axis in enumerate(("x", "y", "z")):
        lo, hi = limits[axis]
        values = [spec.start[i]] + [m.target[i] for m in spec.moves]
        assert min(values) >= lo - 1e-6 and max(values) <= hi + 1e-6
    # Geometry follows the configured envelope; the cycle's timing is unchanged.
    assert spec.geometry["env"] == {"x0": 100.0, "x1": 500.0, "y0": 0.0, "y1": 250.0,
                                    "z0": 50.0, "z1": 300.0}
    assert math.isclose(spec.cycle_time, build_routine(EquipmentType.MILL_3AXIS).cycle_time)


def test_scaled_feed_matches_scaled_travel():
    # Halving X only: a pure-X move's feed halves; durations are untouched, so
    # the emitted Fact stays consistent with the actual (scaled) path speed.
    canned = build_routine(EquipmentType.MILL_3AXIS)
    scaled = build_routine(EquipmentType.MILL_3AXIS, {"x": (0.0, 500.0)})
    prev_c, prev_s = list(canned.start), list(scaled.start)
    for mc, ms in zip(canned.moves, scaled.moves):
        assert ms.duration == mc.duration
        if mc.meta.get("feed"):
            dist_s = math.dist(prev_s[:3], list(ms.target[:3]))
            assert math.isclose(ms.meta["feed"], dist_s / mc.duration, rel_tol=1e-6)
        prev_c, prev_s = list(mc.target), list(ms.target)


def test_trunnion_c_axis_is_continuous():
    # The trunnion's C winds through multiple turns; it is deliberately
    # unlimited (no schema field, no scaling, no Constraints).
    assert "b" in NATIVE_TRAVEL["mill_5axis_trunnion_bc"]
    assert "c" not in NATIVE_TRAVEL["mill_5axis_trunnion_bc"]
    assert "c" not in CREATE_MODELS["mill_5axis_trunnion_bc"].model_fields
    spec = build_routine(EquipmentType.MILL_5AXIS_TRUNNION_BC, {"b": (-30.0, 30.0)})
    c_values = [m.target[4] for m in spec.moves]
    assert max(c_values) > 360.0  # still winds continuously
    b_values = [m.target[3] for m in spec.moves]
    assert min(b_values) >= -30.0 - 1e-6 and max(b_values) <= 30.0 + 1e-6


def test_device_models_publish_constraints():
    from src.simulator import (
        build_mill5_device_model,
        build_mill_device_model,
        build_robot_device_model,
    )

    mill = build_mill_device_model("M", "m.sim", {"x": (0.0, 500.0)})
    assert "<Constraints><Minimum>0</Minimum><Maximum>500</Maximum></Constraints>" in mill

    m5 = build_mill5_device_model("T", "t.sim", {"b": (-90.0, 90.0)})
    assert "<Minimum>-90</Minimum>" in m5
    # C is continuous: its ANGLE item carries no Constraints.
    assert 'name="Cact"  units="DEGREE"  nativeUnits="DEGREE"><Constraints>' not in m5

    robot = build_robot_device_model("R", "r.sim", {"j1": (-45.0, 45.0)})
    assert "<Minimum>-45</Minimum>" in robot
