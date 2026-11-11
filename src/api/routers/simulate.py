"""Simulate session endpoints (docs/api.md §5, §7).

Session creation is one endpoint per equipment type
(``POST /simulate/start/{equipment_type}``), each with a dedicated body model
so every equipment parameter set is fully validated by its own schema — the
catalog's ``create_path`` tells clients where to post. The session lifecycle
endpoints (list/get/delete/routine) are shared across types.
"""

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends

from mtconnect_explorer_client.schemas import (
    EquipmentInfo,
    Mill3AxisCreate,
    Mill5AxisTrunnionBcCreate,
    Robot6DofCreate,
    RoutineInfo,
    SimulateSessionInfo,
)

from src.services.simulate import equipment_catalog, simulate_manager

from ..security import require_api_key

router = APIRouter(
    prefix="/simulate",
    tags=["Simulate"],
    dependencies=[Depends(require_api_key)],
)


@router.get(
    "/equipment",
    summary="Equipment catalog: types, labels, routines, and create endpoints",
    response_model=List[EquipmentInfo],
)
def equipment() -> List[EquipmentInfo]:
    return equipment_catalog()


@router.post(
    "/start/mill_3axis",
    summary="Start a 3-axis mill simulation",
    response_model=SimulateSessionInfo,
    status_code=201,
)
def start_mill_3axis(body: Mill3AxisCreate) -> SimulateSessionInfo:
    return simulate_manager.start(body).to_info(SimulateSessionInfo)


@router.post(
    "/start/mill_5axis_trunnion_bc",
    summary="Start a 5-axis trunnion mill (B/C) simulation",
    response_model=SimulateSessionInfo,
    status_code=201,
)
def start_mill_5axis_trunnion_bc(body: Mill5AxisTrunnionBcCreate) -> SimulateSessionInfo:
    return simulate_manager.start(body).to_info(SimulateSessionInfo)


@router.post(
    "/start/robot_6dof",
    summary="Start a 6-DOF robot simulation",
    response_model=SimulateSessionInfo,
    status_code=201,
)
def start_robot_6dof(body: Robot6DofCreate) -> SimulateSessionInfo:
    return simulate_manager.start(body).to_info(SimulateSessionInfo)


@router.get(
    "/sessions",
    summary="List simulate sessions (including not-yet-purged terminal ones)",
    response_model=List[SimulateSessionInfo],
)
def list_sessions() -> List[SimulateSessionInfo]:
    return simulate_manager.list_info()


@router.get(
    "/sessions/{session_id}",
    summary="One simulate session",
    response_model=SimulateSessionInfo,
)
def get_session(session_id: str) -> SimulateSessionInfo:
    return simulate_manager.get_info(session_id)


@router.get(
    "/sessions/{session_id}/routine",
    summary="Routine spec + start epoch for the phase-locked 3D view",
    response_model=RoutineInfo,
)
def routine(session_id: str) -> RoutineInfo:
    sess = simulate_manager.get(session_id)
    return RoutineInfo(
        spec=sess.spec.to_dict(),
        start_epoch=simulate_manager.start_epoch_of(sess),
        server_time=datetime.now(timezone.utc),
    )


@router.delete(
    "/sessions/{session_id}",
    summary="Stop and remove a simulate session",
    status_code=204,
)
def delete_session(session_id: str) -> None:
    simulate_manager.delete(session_id)


@router.delete(
    "/sessions",
    summary="Stop and remove every simulate session",
    status_code=204,
)
def delete_all_sessions() -> None:
    simulate_manager.delete_all()
