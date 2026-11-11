"""API authentication state (docs/api.md §2).

Two keys are accepted: the static ``api_key`` from config.json (for external
clients) and a per-process runtime token generated here at import (handed to
the in-process Streamlit frontend directly — never written to disk).
"""

import secrets
from typing import Optional, Set

# Generated fresh each process start; the frontend reads it in-process.
runtime_token: str = secrets.token_urlsafe(32)

_api_key: Optional[str] = None
_server_port: int = 8501


def configure(api_key: Optional[str] = None, server_port: Optional[int] = None) -> None:
    """Store the static key and chosen port at startup (called by main)."""
    global _api_key, _server_port
    _api_key = api_key or None
    if server_port is not None:
        _server_port = server_port


def valid_keys() -> Set[str]:
    keys = {runtime_token}
    if _api_key:
        keys.add(_api_key)
    return keys


def server_port() -> int:
    """The HTTP port the app is serving on (for the in-process frontend client)."""
    return _server_port
