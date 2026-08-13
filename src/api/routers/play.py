"""Play session endpoints (docs/api.md §5, §7)."""

from typing import List

from fastapi import APIRouter, Depends

from datetime import datetime, timezone

from mtconnect_explorer_client.schemas import (
    PlayCreate,
    PlayProgress,
    PlaySeek,
    PlaySessionInfo,
)

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


@router.get(
    "/sessions/{session_id}/progress",
    summary="Playhead origin for the scrub bar (duration + pass_start_epoch)",
    response_model=PlayProgress,
)
def progress(session_id: str) -> PlayProgress:
    sess = play_manager.get(session_id)
    p = play_manager.progress_of(sess)
    return PlayProgress(
        duration=p["duration"] or 0.0,
        pass_start_epoch=p["pass_start_epoch"],
        looping=p["looping"],
        paused=p.get("paused", False),
        position=p.get("position"),
        server_time=datetime.now(timezone.utc),
    )


@router.post(
    "/sessions/{session_id}/seek",
    summary="Seek a running playback to a position (requires re-timestamp mode)",
    status_code=204,
)
def seek(session_id: str, body: PlaySeek) -> None:
    play_manager.seek(session_id, body.position)


@router.post(
    "/sessions/{session_id}/pause",
    summary="Pause a running playback (holds the playhead; any timestamp mode)",
    status_code=204,
)
def pause(session_id: str) -> None:
    play_manager.pause(session_id)


@router.post(
    "/sessions/{session_id}/resume",
    summary="Resume a paused playback",
    status_code=204,
)
def resume(session_id: str) -> None:
    play_manager.resume(session_id)


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
