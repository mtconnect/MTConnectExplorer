# mtconnect-explorer-client

Python client and pydantic schemas for the [MTConnectExplorer](../readme.md) REST API.
This package is the single source of truth for the API's request/response models — the
server imports its schemas from here.

## Install

```bash
pip install <path-to>/mtconnect_explorer_client-*.whl
```

(Development installs use `pip install -e ./lib` from the repo root — wired automatically
by `dev.bat` / `make` via `requirements-src.txt`.)

## Usage

```python
from mtconnect_explorer_client import ApiClient, LinearAxisLimits, Mill3AxisCreate

client = ApiClient("http://127.0.0.1:8501/api/v1", api_key="<config.json api_key>")

print(client.version())
print(client.datasets_list())

sid = client.simulate_start(Mill3AxisCreate(x=LinearAxisLimits(min=0.0, max=0.5))).id
for event in client.events_stream(scope="simulate"):
    print(event.seq, event.type)
```

The full endpoint reference lives at `/api/docs` on a running instance, and in
[docs/api.md](../docs/api.md).
