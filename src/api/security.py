"""X-API-Key authentication dependency (docs/api.md §2).

Attached at router level to every router; the sole exception is
``GET /v1/health`` (liveness probes can't send keys). Using a Security
dependency (rather than middleware) also registers the scheme in OpenAPI, so
Swagger's Authorize button can set the key for try-it-out calls.
"""

from typing import Optional

from fastapi import Security
from fastapi.security import APIKeyHeader

from src.services import auth

from .errors import http_error

_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="config.json api_key, or the in-process runtime token.",
)


def require_api_key(api_key: Optional[str] = Security(_api_key_header)) -> None:
    if not api_key or api_key not in auth.valid_keys():
        raise http_error(401, "unauthorized", "missing or invalid API key")
