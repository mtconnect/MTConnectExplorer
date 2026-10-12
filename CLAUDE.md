# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

MTConnectExplorer is an early-stage app for exploring, capturing, replaying, and simulating
[MTConnect](https://github.com/mtconnect/cppagent) machine-tool data, implementing the Testbed as a
Service (TaaS) capture/replay method (see [readme.md](readme.md)). It is a **Streamlit frontend +
FastAPI backend in one process, on one port** (UI at `/`, API at `/api/v1`, Swagger at `/api/docs`),
plus a Python client wheel.

## Commands

All commands assume the repo root. Windows uses `.bat` scripts; Linux uses the `makefile`. The
`venv/` and the bundled MTConnect agent (`src/bin/agent[.exe]`) are **not** committed — the setup
scripts download the agent from the cppagent GitHub releases and build the venv from
[requirements-src.txt](requirements-src.txt), which also editable-installs `lib/`.

| Task | Windows | Linux |
|---|---|---|
| Set up venv + agent + editable lib | `dev.bat` | `make build` |
| Run the app (dev) | `run.bat` | `venv/bin/python -m src` |
| Run the test suite | `venv\Scripts\python.exe -m pytest tests` | `venv/bin/python -m pytest tests` |
| Build a standalone binary | `build.bat` → `dist/MTConnectExplorer.exe` | `make` → `dist/MTConnectExplorer` |
| Build just the client wheel | `venv\Scripts\python.exe -m build --wheel lib` | `venv/bin/python -m build --wheel lib` |
| Clean all generated dirs | `clean.bat` | `make clean` |

- `run.bat` forwards extra args to the app, e.g. `run.bat --port 9000 --host 0.0.0.0`.
- The app must be launched via `python -m src` ([src/main.py](src/main.py)) — it composes the
  Streamlit app and the FastAPI API into one ASGI app (`streamlit.starlette.App` +
  `Mount("/api", …)`). Bare `streamlit run src/app.py` starts the UI **without the API**, so the
  pages (which talk to the API) will not work.
- Release builds (`build.bat` / `make`) build the `lib/` wheel and `pip install --force-reinstall`
  it before PyInstaller, so the frozen binary uses (and proves) the real wheel; `dev.bat` / `make`
  restore the editable install. `pip freeze --exclude mtconnect-explorer-client > requirements.txt`
  keeps the machine-specific editable path out of the lockfile.
- Run a single test: `venv\Scripts\python.exe -m pytest tests/test_datasets.py::test_rename -q`.
  Tests needing the bundled agent self-skip when `src/bin/agent[.exe]` is missing (so CI's `test`
  job runs without it).

### Integration tests (pre-API, still authoritative for the data path)

Three end-to-end flows in [src/test.py](src/test.py), each a CLI flag `sys.exit()`-ing pass/fail:

```bash
python -m src --loopback-test   # PRIMARY correctness check: data → agent → capture → diff round-trip, no network
python -m src --mirror-test     # mirrors https://demo.mtconnect.org/ through a local agent
python -m src --play-test       # replays a captured dataset through a local agent
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs `pytest` (ubuntu, agent tests
auto-skip) and verifies the app builds on Linux and Windows; it does **not** run the flows above.

### Headless SHDR-log conversion

```bash
python -m src --convert-shdr-log adapter.log --devices Devices.xml --out datasets/out.jsonl
```

## Architecture

### The core insight: this app plays both sides of MTConnect

An MTConnect deployment is *SHDR adapter → agent (cppagent) → HTTP clients*. MTConnectExplorer acts
as an **HTTP client** (Explore/Capture) *and* as an **SHDR adapter + agent host** (Play/Simulate).
For Play/Simulate it launches the bundled `agent[.exe]` in a temp dir with a generated `agent.cfg` +
`Devices.xml`, and the app's own `ShdrAdapter` (a TCP server) is the SHDR source that agent connects
to. Downstream tools then consume the local agent over HTTP.

### The three-layer split (+ the client wheel)

```
lib/                    mtconnect-explorer-client wheel — pydantic schema home + sync httpx ApiClient
src/services/           Streamlit-free domain managers (sessions, datasets, ports, events, auth)
src/api/                FastAPI routers (thin wrappers over services), mounted at /api
src/pages/              Streamlit frontend — talks to the API via the lib client (Explore excepted)
src/{client,shdr,agent,converter,dataset,simulator,viz}.py   domain code (unchanged data path)
```

**Boundary rules (enforced by review, not tooling):**

- Nothing under `src/services/` or `src/api/` may import `streamlit`.
- `lib/` imports nothing from `src/` (the dependency points the other way: services/api/pages all
  import schemas from `mtconnect_explorer_client.schemas` — the single source of truth for the wire
  contract).
- Pages import only the lib client + [src/pages/core.py](src/pages/core.py) view-state helpers.
  [src/pages/api.py](src/pages/api.py) is the one place the frontend touches `src.services` (to
  read the runtime token + port). **Explore is the documented carve-out**: it keeps a direct
  `MtConnectClient`/`MtConnectExplorer` because an MTConnect agent is itself a REST API.
- [docs/api.md](docs/api.md) is the normative API contract; it wins over any other document
  (including this one) on API details.

### The API (docs/api.md)

- One process/port: `streamlit.starlette.App(script_path, routes=[Mount("/api", create_api())])`
  in [src/main.py](src/main.py). One `APIRouter` per service → one Swagger tag each (System,
  Datasets, Capture, Play, Simulate, Events), `summary=` on every route.
- **Auth:** `X-API-Key` ∈ {config.json `api_key`, per-process runtime token
  ([src/services/auth.py](src/services/auth.py), handed to the in-proc frontend)}. `/api/v1/health`
  and the docs pages are unauthenticated. Default bind `127.0.0.1`; a non-local `--host` without an
  `api_key` **refuses to start**.
- **Trusted-LAN only — never expose to the internet.** The `api_key` gates only `/api`; the
  **Streamlit UI has no authentication** and drives the same backend via the runtime token, so an
  exposed instance is fully operable by anyone who can reach it (and Capture fetches
  arbitrary operator-supplied `base_url`s server-side — an SSRF vector). The startup banner states
  this; the `--host` gate is a guard, not a safety guarantee.
- **Errors:** services raise typed [src/services/errors.py](src/services/errors.py) exceptions;
  the API maps them onto `HTTPException(status, detail={"code","message","context"})`.
- **Simulate:** session creation is **one endpoint per equipment type** —
  `POST /simulate/start/{mill_3axis|mill_5axis_trunnion_bc|robot_6dof}` — each with a dedicated
  pydantic body so every parameter set is fully validated (a deliberate RPC-style exception to the
  resource-REST rule; docs/api.md decision #12). Axis **limits** are meters (linear) / degrees
  (rotary), default to native travel, and scale the routine + 3D geometry + published MTConnect
  `Constraints`. Catalog entries carry a `create_path`; the generic `POST /simulate/sessions` was removed.
- **Play scrub/seek:** `GET /play/sessions/{id}/progress` returns the playhead origin
  (`duration` + `pass_start_epoch` + `server_time`); `POST /play/sessions/{id}/seek` seeks a running
  playback (requires re_timestamp — see the scrub subsection below).
- **Events:** one global bus ([src/services/events.py](src/services/events.py)) — monotonic `seq`,
  10k ring buffer, poll (`after` cursor + `dropped`) and NDJSON `/events/stream`. Log records are
  ingested from the `src` logger; the `\x1f` thread-name scheme attributes them to sessions
  (thread names carry the *session id*; labels are looked up at render time). **Stream clients must
  send `Accept-Encoding: identity`** — a gzip layer buffers quiet streams (the lib client does this
  automatically).
- **Datasets:** multipart `POST /datasets/upload`, 1 MB-chunked temp file + atomic move,
  **no overwrite** (409), path-traversal guard — all in
  [src/services/datasets.py](src/services/datasets.py), the sole owner of `datasets/`.

### Sessions & the worker-host seam

All three scopes (capture/play/simulate) share one lifecycle
(`starting → running → stopping → finished|error`) implemented in
[src/services/sessions.py](src/services/sessions.py): global ids (`cap-1`, `play-2`, `sim-3`), one
`STATE_LOCK` for atomic port validate-and-reserve across scopes, terminal sessions retained 15 min
(the frontend auto-deletes them to keep the instant-reap UX). Ports are **derived from the live
session table** ([src/services/ports.py](src/services/ports.py)) — no reservation store to leak.

A worker is a pure function `fn(config: BaseModel, ctx: WorkerContext)` registered in
[src/services/hosts.py](src/services/hosts.py) and started through the `WorkerHost` seam. The
default host is **`SubprocessHost`** — each worker self-execs the app
(`MTConnectExplorer --worker <kind>` frozen, `python -m src --worker <kind>` in dev; dispatched in
main.py before any streamlit import) so each session's tick loop gets its own GIL and a worker
crash can't take the server down. Protocol ([src/services/worker_main.py](src/services/worker_main.py)):
config = one JSON line on stdin; `{"cmd":"stop"}` or stdin EOF (orphan protection) = graceful stop;
the child emits NDJSON `log`/`status`/`exit` records on stdout, which the parent forwards to the
event bus and manager. Flip a scope back to in-process threads by constructing its manager with
`ThreadHost()` (one line per scope module). Workers report transitions via `ctx.set_status(...)` —
e.g. simulate publishes `start_epoch` in the `running` event's detail, which is how the 3D view
gets its phase-lock origin without reaching into the worker.

The seam is **bidirectional**: `ctx.poll_command()` drains inbound commands delivered by
`SessionManager.send_command(sid, cmd)` — in-process for `ThreadHost`, or an extra stdin
`{"cmd":…}` line (serialized against the stop-writer) for `SubprocessHost`, decoded by
`worker_main.py`'s stdin reader. Play's seek is the first user of this channel.

### The 3D view stays synced without data

The simulate routine is deterministic: `GET /simulate/sessions/{id}/routine` returns
`RoutineSpec.to_dict()` + `start_epoch` + `server_time`, and [src/viz.py](src/viz.py) re-runs the
identical piecewise-linear interpolation in JS from `Date.now()` (corrected by the clock offset
computed from `server_time`) — sync is mathematical, not data-communicated.

### Play scrubbing (seek) — the two-clock rule

The Play scrub bar reuses the same origin trick: the worker publishes `pass_start_epoch` + `duration`
(+ `paused` + `position`) on start/loop-wrap/seek/pause (not per tick — no chatty bus), and the UI
computes the live playhead as `server_time − pass_start_epoch` (or holds it at `position` while
paused). The player ([src/converter.py](src/converter.py)) models two decoupled clocks:

- a **playhead target** (`_pos_anchor`/`_wall_anchor`): advances at 1× wall-clock while playing,
  frozen while paused, set directly by a seek. A frame at in-pass offset `O` emits once the target
  reaches `O` — **unpaced when behind** (a post-seek catch-up), **paced when caught up**. So
  **pause just freezes the target** and never blocks the emit loop: a post-seek fast-forward runs to
  the frozen target and then idles (`pause()`/`resume()` work in any timestamp mode).
- an **emit-timestamp clock** (`_emit_origin` + monotonic guard), kept separate so a backward
  seek/loop-wrap never emits a decreasing timestamp (the agent rejects those). That only holds when
  timestamps are rewritten, so **seek requires `re_timestamp=True`** (UI disables the bar otherwise;
  backend rejects `409 seek_unavailable`).

**Seek is snapshot-indexed, not a full rewind.** At `open()` the player pre-scans the recording once,
folding every observation into running device state (`_DeviceStateFold`) and recording a full-state
snapshot (a `/current` equivalent) every `snapshot_interval_s` dataset seconds — each with the byte
offset of the following frame ([DatasetReader.open_at](src/dataset.py) seeks there; offsets are
line-start cookies so there's no partial-line reassembly). A seek jumps to the nearest snapshot ≤ the
target, emits that state, then fast-forwards only the ≤`snapshot_interval_s` of remaining deltas —
**same path forward and backward**, but bounded instead of replaying from the start.
`snapshot_interval_s` (default 15) is a `PlayCreate` field / "Snapshot Interval" launch setting,
fixed once playing.

### Streamlit view-state model

`st.session_state` is per-browser-session, so frontend **view** state (form defaults, tree
selections) lives in a process-global `@st.cache_resource` store in
[src/pages/core.py](src/pages/core.py), shared across users and guarded by an `RLock`. Domain state
lives behind the API. Log panes render per-scope `ScopeFeed`s ([src/pages/api.py](src/pages/api.py))
— background threads consuming `/events/stream` into bounded deques, merged with frontend `[ui]`
lines. Capture/Play/Simulate pages each run a `@st.fragment(run_every=…)` **watcher** that polls a
coarse backend fingerprint `(id, status)` and triggers a full-page rerun only on change, so
API-driven starts/stops appear without touching the page — the fingerprint deliberately excludes
fast-moving values (e.g. the play position) so a quiet backend never reruns the page or dismisses an
open dialog. The play scrub bar advances in its own per-session fragment (see the seek subsection);
a known wart of the native `st.slider` is an occasional mid-drag thumb snap-back at the ~1 s tick.

### Dataset formats (the header string selects the schema)

`DatasetReader`/`DatasetWriter` ([src/dataset.py](src/dataset.py)) are constructed with a header
string that must match the file's first line. Two schemas are in use:

- **`RawMTConnect-v0.1`** — payload is a `MtConnectClientFrame` (a request + its full raw XML
  response). Written by Capture; read by Play and both converters. First frame **must** be a `probe`.
- **`Shdr-v0.1`** — payload is a single SHDR observation (`ID` = the observation class name).
  Produced by `XmlToShdrConverter.convert_file` and the loopback-test generator; consumed by
  `ShdrDataPlayer`.

### Versioning & release

`__version__` in [src/__version__.py](src/__version__.py) is the single source of truth for the
app; `lib/src/mtconnect_explorer_client/__version__.py` mirrors it in PEP 440 form (`0.1.0-alpha.5`
→ `0.1.0a5`) — **bump both together**. Bumping on a `release/*` branch *is* cutting a release; the
branch model and rulesets are documented in [contributing.md](contributing.md) — read it before
touching branches, `__version__`, or the workflows.

## Gotchas

- **The bundled agent reads a float with a trailing `.0` back as an int**, breaking SHDR loopback
  type round-tripping. Values are stripped of a trailing `.0` before emission — see `_fmt()` in
  [simulator.py](src/simulator.py) and `add_observation_if_changed` in [test.py](src/test.py).
- **Use `127.0.0.1`, not `localhost`**, for adapter/agent hosts — IPv6 resolution causes lag on
  Windows (noted in [agent.py](src/agent.py)).
- **Start the SHDR adapters before the agent** when a session fronts **multiple** adapters. If the
  bundled agent starts before any adapter is listening, on its connection-retry sweep it
  deterministically skips the **first-declared adapter**, so an N-device session connects to only
  N-1 and hangs in `starting` at the connect-wait. [play_worker](src/services/play.py) opens all
  adapters first, then starts the agent; single-adapter simulate/loopback are immune (which is why
  it went unnoticed). The connect-wait is bounded and raises with the un-connected device names.
- Paced players guard against corrupt/misordered data: `ShdrDataPlayer`/`DatasetPlayer` reject a
  frame >120 s from the pass start; `MtConnectDatasetPlayer` rejects a >10 min gap between frames.
- `config.json` (optional, gitignored) is parsed as JSONC — comments are stripped before pydantic
  validation ([src/services/config.py](src/services/config.py)); read with `utf-8-sig` so a
  Windows editor's UTF-8 BOM doesn't break parsing. Fields: `log_level`, `api_key`.
- **Streamlit `st.slider` + write-before-create is a trap**: re-seeding a slider's value via
  `session_state` each tick fires its `on_change`, so an `on_change` seek handler self-triggers a
  feedback loop. The play scrub bar instead compares the slider's committed value to the exact value
  it last wrote (both rounded to the step) to tell a real drag from the programmatic re-seed.
- `CLAUDE.md`, `.claude/`, `config.json`, `datasets/`, `dist/`, `build/`, `src/bin/`, `test/`
  (but **not** `tests/`), and `venv/` are all gitignored.
- Both build entry points use the same [installer.spec](installer.spec) (PyInstaller), which bundles
  all of `src/` and reads `__version__` to stamp Windows file-version fields.
