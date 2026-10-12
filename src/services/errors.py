"""Typed service errors (docs/api.md §3).

Managers raise these; the API layer maps them 1:1 onto
``HTTPException(status, detail={"code", "message", "context"})`` and the
Streamlit frontend renders ``message`` directly.
"""

from typing import Any, Dict, Optional


class ServiceError(Exception):
    status: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.context = context

    def detail(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.context is not None:
            d["context"] = self.context
        return d


class NotFound(ServiceError):
    status = 404
    code = "not_found"


class Conflict(ServiceError):
    status = 409
    code = "conflict"


class Invalid(ServiceError):
    status = 422
    code = "validation_error"


class TooLarge(ServiceError):
    status = 413
    code = "too_large"


class UnsupportedType(ServiceError):
    status = 415
    code = "unsupported_type"
