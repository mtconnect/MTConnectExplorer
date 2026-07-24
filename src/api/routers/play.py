"""Play session endpoints (docs/api.md §5, §7)."""

from typing import List

from fastapi import APIRouter, Depends

from mtconnect_explorer_client.schemas import PlayCreate, PlaySessionInfo

from src.services.play import play_manager

from ..security import require_api_key

router = APIRouter(
    prefix="/play",
    tags=["Play"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/sessions",
    summary="Start a playback session (dataset + ports validated atomically)",
    response_model=PlaySessionInfo,
    status_code=201,
)
def create_session(body: PlayCreate) -> PlaySessionInfo:
    return play_manager.start(body).to_info(PlaySessionInfo)


@router.get(
    "/sessions",
    summary="List play sessions (including not-yet-purged terminal ones)",
    response_model=List[PlaySessionInfo],
)
def list_sessions() -> List[PlaySessionInfo]:
    return play_manager.list_info()


@router.get(
    "/sessions/{session_id}",
    summary="One play session",
    response_model=PlaySessionInfo,
)
def get_session(session_id: str) -> PlaySessionInfo:
    return play_manager.get_info(session_id)


@router.delete(
    "/sessions/{session_id}",
    summary="Stop and remove a play session",
    status_code=204,
)
def delete_session(session_id: str) -> None:
    play_manager.delete(session_id)


@router.delete(
    "/sessions",
    summary="Stop and remove every play session",
    status_code=204,
)
def delete_all_sessions() -> None:
    play_manager.delete_all()
