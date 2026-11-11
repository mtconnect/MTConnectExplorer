"""Typed API errors raised by the MTConnectExplorer client (docs/api.md §3)."""

from typing import Any, Dict, Optional

import httpx


class ApiError(Exception):
    """A non-2xx API response, carrying the typed error detail.

    ``code``/``message``/``context`` mirror the server's
    ``{"detail": {"code", "message", "context"}}`` object. FastAPI-native 422
    validation errors (where ``detail`` is a list of field errors) are mapped
    to code ``validation_error`` with the list in ``context["errors"]``.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"[{status_code} {code}] {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.context = context

    @classmethod
    def from_response(cls, response: httpx.Response) -> "ApiError":
        status = response.status_code
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = None
        if isinstance(detail, dict) and "code" in detail:
            return cls(
                status,
                str(detail.get("code")),
                str(detail.get("message", "")),
                detail.get("context"),
            )
        if isinstance(detail, list):  # FastAPI-native 422 validation format
            first = detail[0] if detail else {}
            return cls(
                status,
                "validation_error",
                str(first.get("msg", "request validation failed")),
                {"errors": detail},
            )
        return cls(status, "http_error", response.text[:500] or f"HTTP {status}")
