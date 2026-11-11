"""Capture session endpoints (docs/api.md §5, §7)."""

from typing import List

from fastapi import APIRouter, Depends

from mtconnect_explorer_client.schemas import CaptureCreate, CaptureSessionInfo

from src.services.capture import capture_manager

from ..security import require_api_key

router = APIRouter(
    prefix="/capture",
    tags=["Capture"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/sessions",
    summary="Start a capture session (validated atomically)",
    response_model=CaptureSessionInfo,
    status_code=201,
)
def create_session(body: CaptureCreate) -> CaptureSessionInfo:
    return capture_manager.start(body).to_info(CaptureSessionInfo)


@router.get(
    "/sessions",
    summary="List capture sessions (including not-yet-purged terminal ones)",
    response_model=List[CaptureSessionInfo],
)
def list_sessions() -> List[CaptureSessionInfo]:
    return capture_manager.list_info()


@router.get(
    "/sessions/{session_id}",
    summary="One capture session",
    response_model=CaptureSessionInfo,
)
def get_session(session_id: str) -> CaptureSessionInfo:
    return capture_manager.get_info(session_id)


@router.delete(
    "/sessions/{session_id}",
    summary="Stop and remove a capture session",
    status_code=204,
)
def delete_session(session_id: str) -> None:
    capture_manager.delete(session_id)


@router.delete(
    "/sessions",
    summary="Stop and remove every capture session",
    status_code=204,
)
def delete_all_sessions() -> None:
    capture_manager.delete_all()
