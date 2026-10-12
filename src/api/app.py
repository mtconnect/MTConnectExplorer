"""FastAPI application factory (docs/api.md §1–§3).

Mounted under ``/api`` in main.py, so routes live at ``/api/v1/...`` and the
interactive docs at ``/api/docs`` / ``/api/redoc`` / ``/api/openapi.json``.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.__version__ import __version__
from src.services.errors import ServiceError

from .routers import routers

logger = logging.getLogger(__name__)


def create_api() -> FastAPI:
    api = FastAPI(
        title="MTConnectExplorer",
        version=__version__,
        description=(
            "REST API for MTConnectExplorer: capture, play, and simulate "
            "sessions; datasets; events/logs; ports; running agents. "
            "Authenticate with the `X-API-Key` header (config.json `api_key`); "
            "`GET /v1/health` is the only unauthenticated endpoint."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    @api.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail()})

    @api.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(f"unhandled API error on {request.url.path}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"detail": {"code": "internal_error", "message": str(exc)}},
        )

    for router in routers:
        api.include_router(router, prefix="/v1")

    return api
