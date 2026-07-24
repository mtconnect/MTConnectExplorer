"""API test fixtures.

The API is served by a real uvicorn server on an ephemeral port in a daemon
thread, and exercised through the lib's ApiClient over real HTTP. (The client
is sync httpx, which cannot drive httpx.ASGITransport — that transport is
async-only — and a real server also covers the NDJSON streaming path.)
"""

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

API_KEY = "test-api-key"

AGENT_BINARY = REPO_ROOT / "src" / "bin" / ("agent.exe" if sys.platform == "win32" else "agent")

requires_agent = pytest.mark.skipif(
    not AGENT_BINARY.exists(),
    reason="bundled MTConnect agent not present (run dev.bat / make build)",
)


@pytest.fixture(scope="session")
def server() -> str:
    """Base URL of a live API server (FastAPI app under uvicorn, ephemeral port)."""
    from src.api.app import create_api
    from src.services import auth, datasets

    datasets.set_base_dir(Path(tempfile.mkdtemp(prefix="mtce-test-")) / "datasets")
    auth.configure(api_key=API_KEY)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(create_api(), host="127.0.0.1", port=port, log_level="warning")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not uv_server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn test server failed to start")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    uv_server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def client(server):
    from mtconnect_explorer_client import ApiClient

    c = ApiClient(f"{server}/v1", api_key=API_KEY)
    yield c
    c.close()


@pytest.fixture()
def clean_datasets(server):
    """Empty the (test-scoped) datasets directory before and after the test."""
    from src.services import datasets

    def wipe():
        for f in datasets.ensure_dir().iterdir():
            f.unlink()

    wipe()
    yield
    wipe()
