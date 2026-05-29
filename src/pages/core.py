import inspect
import logging
import os
import queue
from pathlib import Path
from typing import Literal, Optional

import streamlit as st
from pydantic import BaseModel, Field

from src.agent import AgentConfig, AgentAdapter
from src.client import MtConnectClientConfiguration, MtConnectDataCollectorConfiguration
from src.shdr import ShdrDataPlayer

DATASETS_DIR = Path("datasets")
_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# ---------------------------------------------------------------------------
# Logging setup — mirrors src/main.py Config class and config.json logic
# ---------------------------------------------------------------------------
class _Config(BaseModel):
    log_level: Literal["debug", "info", "warning", "error", "critical"] = Field("info")

    def get_log_level_value(self) -> int:
        return logging._nameToLevel[self.log_level.upper()]


_log_level = (
    _Config.model_validate_json(Path("config.json").read_text()).get_log_level_value()
    if os.path.isfile("config.json")
    else _Config().get_log_level_value()
)
logging.basicConfig(level=_log_level, format=_LOG_FMT)
logging.getLogger("src").setLevel(_log_level)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.queue = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


def attach_log(q: queue.Queue) -> _QueueHandler:
    h = _QueueHandler(q)
    h.setFormatter(logging.Formatter(_LOG_FMT))
    logging.getLogger("src").addHandler(h)
    return h


def detach_log(h: Optional[_QueueHandler]) -> None:
    if h is not None:
        logging.getLogger("src").removeHandler(h)


def drain(q: queue.Queue, lines: list) -> None:
    while True:
        try:
            lines.append(q.get_nowait())
        except queue.Empty:
            break


_LOG_AREA_CSS = """
<style>
div[data-testid="stTextArea"] textarea {
    min-height: 200px !important;
    height: calc(100vh - 480px) !important;
}
</style>
"""


def log_area(key: str, lines: list) -> None:
    text = "\n".join(lines)
    st.session_state[key] = text
    st.text_area("Log", value=text, height=200, key=key, label_visibility="collapsed", disabled=True)
    st.markdown(_LOG_AREA_CSS, unsafe_allow_html=True)
    # Include len(lines) so this HTML differs on each update, forcing Streamlit
    # to re-render the iframe and re-execute the script. Double rAF fires after
    # React has committed its DOM changes, ensuring scrollTop is set last.
    st.iframe(
        f"""<script>
        /* n={len(lines)} */
        window.parent.requestAnimationFrame(function() {{
            window.parent.requestAnimationFrame(function() {{
                var areas = window.parent.document.querySelectorAll('textarea');
                areas.forEach(function(a) {{ a.scrollTop = a.scrollHeight; }});
            }});
        }});
        </script>""",
        height=1,
    )
