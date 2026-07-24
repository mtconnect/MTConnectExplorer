"""Application configuration (config.json, parsed as JSONC) and logging setup.

Consolidates the config/logging bootstrap previously duplicated in
src/pages/core.py and src/test.py.
"""

import logging
import os
import re
import threading
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class Config(BaseModel):
    log_level: Literal["debug", "info", "warning", "error", "critical"] = Field("info")
    api_key: Optional[str] = Field(
        None,
        description="Static API key accepted by the HTTP API (X-API-Key header). "
        "Required to bind the server to a non-local host.",
    )

    def get_log_level_value(self) -> int:
        return logging._nameToLevel[self.log_level.upper()]


def strip_json_comments(text: str) -> str:
    """Strip // line and /* */ block comments from JSON text so config.json can
    be commented like JSONC. Comment sequences inside string values (e.g. a
    "http://..." URL) are preserved: string literals are matched first and kept."""
    return re.sub(
        r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
        lambda m: m.group(0) if m.group(0).startswith('"') else "",
        text,
        flags=re.DOTALL,
    )


def load_config(path: Union[str, Path] = "config.json") -> Config:
    """Load config.json (JSONC) if present, else defaults."""
    if os.path.isfile(path):
        return Config.model_validate_json(strip_json_comments(Path(path).read_text()))
    return Config()


_setup_lock = threading.Lock()
_setup_done = False


def setup_logging(config: Optional[Config] = None) -> int:
    """Configure root logging from config.json (idempotent). Returns the level."""
    global _setup_done
    level = (config or load_config()).get_log_level_value()
    with _setup_lock:
        if not _setup_done:
            logging.basicConfig(level=level, format=LOG_FMT)
            logging.getLogger("src").setLevel(level)
            _setup_done = True
    return level
