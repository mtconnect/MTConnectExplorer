"""HTTP error helpers (docs/api.md §3): every non-2xx response carries a typed
object inside FastAPI's native ``detail`` field."""

from typing import Any, Dict, Optional

from fastapi import HTTPException

from src.services.errors import ServiceError


def http_error(
    status: int,
    code: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> HTTPException:
    detail: Dict[str, Any] = {"code": code, "message": message}
    if context is not None:
        detail["context"] = context
    return HTTPException(status_code=status, detail=detail)


def from_service_error(ex: ServiceError) -> HTTPException:
    """Map a services-layer error 1:1 onto its HTTP form."""
    return HTTPException(status_code=ex.status, detail=ex.detail())
