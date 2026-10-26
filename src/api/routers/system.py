"""System endpoints (docs/api.md §4)."""

from typing import List

from fastapi import APIRouter, Depends, Query

from mtconnect_explorer_client.schemas import (
    AgentInfo,
    HealthInfo,
    PortsInfo,
    VersionInfo,
)

from src.__version__ import __version__
from src.services import ports

from ..security import require_api_key

router = APIRouter(tags=["System"])


@router.get(
    "/health",
    summary="Liveness probe (the only unauthenticated endpoint)",
    response_model=HealthInfo,
)
def health() -> HealthInfo:
    return HealthInfo(status="ok")


@router.get(
    "/version",
    summary="Application and API version",
    response_model=VersionInfo,
    dependencies=[Depends(require_api_key)],
)
def version() -> VersionInfo:
    return VersionInfo(version=__version__, api="v1")


@router.get(
    "/agents",
    summary="Local MTConnect agents run by live Play/Simulate sessions",
    response_model=List[AgentInfo],
    dependencies=[Depends(require_api_key)],
)
def agents() -> List[AgentInfo]:
    return ports.running_agents()


@router.get(
    "/ports",
    summary="Used ports plus free suggestions for pre-filling launch forms",
    response_model=PortsInfo,
    dependencies=[Depends(require_api_key)],
)
def ports_info(
    shdr_count: int = Query(1, ge=0, le=64, description="How many SHDR ports to suggest."),
) -> PortsInfo:
    return ports.suggest_ports(shdr_count)
