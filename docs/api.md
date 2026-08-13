# MTConnectExplorer HTTP API — v1

> **Status: IMPLEMENTED** (2026-07-24). This document is the normative wire
> contract; the generated OpenAPI at `/api/openapi.json` (docs at `/api/docs`)
> reflects it. The pydantic schemas live in the `mtconnect-explorer-client`
> wheel (`lib/`), which also ships the reference Python client.

The API exposes everything the backend owns: **capture, play, and simulate
sessions; datasets; logs; ports; running agents**. It is served by the same
process as the Streamlit UI (UI at `/`, API at `/api`, interactive docs at
`/api/docs`).

**Deliberately not in v1** (documented so they read as decisions, not gaps):

- **Live exploration.** An MTConnect agent is already a REST API; clients
  explore by calling the agent's own `probe`/`current`/`sample` endpoints
  directly — exactly what the built-in Explore page does.
- SHDR-log → dataset conversion endpoint (CLI only for now).
- WebSockets/SSE (NDJSON-over-HTTP streaming covers v1 needs).
- Pagination (list sizes are small; revisit if that changes).

---

## 1. Conventions

| Topic | Convention |
|---|---|
| Base path | `/api/v1` |
| Auth | `X-API-Key: <key>` header on every request (see §2). |
| Request/response bodies | JSON (pydantic models end-to-end; OpenAPI generated from them). |
| Streaming | `application/x-ndjson` — one JSON object per line (see §8). |
| Dataset content | `application/octet-stream`, streamed. |
| Timestamps | RFC 3339 UTC with milliseconds: `2026-07-23T18:04:11.234Z`. |
| Session IDs | Opaque strings, prefixed by scope: `cap-1`, `play-2`, `sim-3`. |
| Scopes | `capture`, `play`, `simulate`. |
| Errors | Typed object inside FastAPI-native `detail` (see §3). |
| OpenAPI tags | One Swagger tag per service — `System`, `Datasets`, `Capture`, `Play`, `Simulate`, `Events` — implemented as one `APIRouter` per tag, so `/api/docs` groups endpoints cleanly by service. |
| Reference style | Follows the AMT-DTP ISE/ISCP service layout: router-per-service with `prefix` + `tags`, `summary=` on every route, docs at `/api/docs` + `/api/redoc` + `/api/openapi.json`, `title`/`version` from `__version__`, and a global unhandled-exception handler. Known deviations: resource-REST paths with ids in path segments (vs. the reference's RPC-style `/list`/`/open` with ids in bodies), and API-key auth (reference has none). |

## 2. Authentication

> **Deployment posture: trusted LAN only — never the internet.** The `api_key`
> below gates the `/api` endpoints, **not the Streamlit UI**, which has no
> authentication at all. The UI drives the same backend (via the runtime
> token), so anyone who can reach an exposed instance can operate it regardless
> of the key. The refuse-to-start guard forces a key when binding beyond
> localhost, but that hardens only the direct-API surface — it does not make an
> internet-facing deployment safe. One key sharp edge: Capture issues HTTP
> requests to an arbitrary operator-supplied `base_url` server-side (an SSRF
> primitive on an exposed host). Run this only on a network where every machine
> that can reach it is trusted.

- **Static key** — `api_key` field in `config.json` (JSONC). For external
  clients and any future remote UI.
- **Runtime token** — generated at startup, handed to the in-process Streamlit
  frontend directly (never written to disk). Middleware accepts either key.
- Default bind is `127.0.0.1`. Exposing requires an explicit `--host`.
- **Resolved:** non-local `--host` with no `api_key` configured → the process
  **refuses to start**.
- **Resolved:** `GET /api/v1/health` is the *only* unauthenticated API
  endpoint (liveness probes can't send keys).
- The interactive reference pages — `/api/docs`, `/api/redoc`,
  `/api/openapi.json` — are likewise served without a key (a browser can't
  send headers to read documentation); Swagger's **Authorize** button carries
  the key for try-it-out calls. They live outside `/api/v1` and expose no data.

Auth failures return `401` with error code `unauthorized`.

## 3. Error model

Every non-2xx response carries a **typed object inside FastAPI's native
`detail` field** — harmonized with the AMT-DTP services (which use `detail`
strings) while keeping machine-readable codes:

```json
{
  "detail": {
    "code": "port_conflict",
    "message": "agent port 5000 is in use by play-2",
    "context": {"conflicts": [{"port": 5000, "held_by": "play-2", "kind": "agent"}]}
  }
}
```

Route code raises plain `HTTPException(status_code, detail={...})`. A global
unhandled-exception handler (adopted from the reference services) returns
`500` with `{"detail": {"code": "internal_error", "message": "..."}}`.
Exception: FastAPI's built-in `422` validation errors keep their native format
(`detail` is a *list* of field errors) — clients should handle `detail` being
either the typed object or that list.

| HTTP | `code` | When |
|---|---|---|
| 401 | `unauthorized` | Missing/invalid API key |
| 404 | `session_not_found` | Unknown session id (incl. already purged) |
| 404 | `dataset_not_found` | Unknown dataset name |
| 409 | `port_conflict` | Requested port(s) held by a live session (`detail.conflicts` lists each) |
| 409 | `dataset_in_use` | Dataset is being played/captured (rename/delete/overwrite/play-while-capturing) |
| 409 | `dataset_exists` | Upload/rename target already exists (overwriting is not allowed) |
| 413 | `too_large` | Upload exceeds configured cap (default 5 GiB) |
| 415 | `unsupported_type` | Upload extension/MIME is not a `.jsonl` dataset |
| 422 | `validation_error` | Pydantic validation — FastAPI-native format (`detail` is a list of field errors) |
| 422 | `invalid_dataset` | File exists but header/probe frame is malformed |
| 422 | `invalid_name` | Filename fails sanitization (§6) |

## 4. System

### `GET /api/v1/health`   *(unauthenticated)*
```json
{"status": "ok"}
```

### `GET /api/v1/version`
```json
{"version": "0.1.0-alpha.5", "api": "v1"}
```

### `GET /api/v1/agents`

Base URLs of local MTConnect agents run by live Play/Simulate sessions
(feeds the Explore/Capture quick-fill buttons). De-duplicated.

```json
[
  {"base_url": "http://localhost:5000/", "sessions": ["play-2"]},
  {"base_url": "http://localhost:5001/", "sessions": ["sim-1"]}
]
```

### `GET /api/v1/ports?shdr_count=3`

Used ports across all live sessions plus free suggestions for pre-filling
launch forms.

```json
{
  "used": [5000, 7878, 7879],
  "suggested": {"agent": 5001, "shdr": [7880, 7881, 7882]}
}
```

**Port lifecycle:** the used-set is *derived from the live session table* —
there is no separate reservation store to leak. A session holds its ports
through every non-terminal state **including `stopping`**; the moment it
reaches `finished`/`error` or is deleted, its ports are free. Reservation is
atomic inside session creation (single manager lock), so two concurrent `POST`s
cannot both claim the same port — the loser gets `409 port_conflict`.

## 5. Sessions (common model)

All three scopes share one lifecycle and envelope.

```json
{
  "id": "play-2",
  "scope": "play",
  "label": "demo.jsonl | Mazak | localhost:5000",
  "status": "running",
  "created_at": "2026-07-23T18:03:59.101Z",
  "started_at": "2026-07-23T18:04:11.234Z",
  "finished_at": null,
  "error": null,
  "ports": {"agent": 5000, "shdr": [7878]},
  "config": { "…scope-specific config echo…": "…" }
}
```

The `config` echo is **typed per scope** (`CaptureSessionInfo` /
`PlaySessionInfo` / `SimulateSessionInfo` subclass the common `SessionInfo`),
so OpenAPI and the client library expose the exact create-body shape back.

**Status machine:** `starting → running → (stopping →) finished | error`

- `starting` — worker launched; agent booting / connecting to SHDR adapter(s).
- `running` — data flowing (Play's `started_event`; Simulate's `start_epoch` set;
  Capture's `is_collecting`). "Looping" is not a status — clients derive it
  from `config.loop == true && status == running`.
- `error` — worker died; `error` field carries the message (today this only
  reaches logs).

**Lifecycle endpoints (per scope; `{s}` = `capture` | `play` | `simulate`):**

| Endpoint | Behavior |
|---|---|
| `POST /api/v1/{s}/sessions` | Validate (ports, dataset, …) atomically → `201` + session envelope, or `409`/`404`/`422`. |
| `GET /api/v1/{s}/sessions` | List (includes terminal sessions not yet purged). |
| `GET /api/v1/{s}/sessions/{id}` | Single session. `404` after purge/delete. |
| `DELETE /api/v1/{s}/sessions/{id}` | Stop (signal, join with timeout) and remove → `204`. Idempotent-ish: `404` if unknown. |
| `DELETE /api/v1/{s}/sessions` | Stop-all for the scope → `204`. |

**Resolved — terminal retention:** the *primary* completion signal is the
`session` status event on the event bus (§8) — streaming and cursor-polling
clients are notified without relying on retention at all. Terminal sessions
additionally **remain listed** until `DELETE` or auto-purge after **15
minutes** as a REST-ergonomics safety net, so plain request/response clients
(curl, scripts) can still query the outcome. The frontend auto-`DELETE`s
terminal sessions to preserve today's instant-reap look.

## 6. Datasets

Names must end in `.jsonl` and pass sanitization: no path separators, no
`< > : " / \ | ? *`, no leading dots; the resolved path must remain inside the
datasets directory (traversal guard). Violations → `422 invalid_name`.

### `GET /api/v1/datasets`
```json
[
  {
    "name": "demo.jsonl",
    "size_bytes": 73400320,
    "modified": "2026-07-23T14:02:11.000Z",
    "in_use": ["play"]
  }
]
```
`in_use` ⊆ `["play", "capture"]` — empty when idle. (This replaces today's
cross-page `_dataset_status()` reach-around with one authoritative answer.)

### `POST /api/v1/datasets/upload`

Multipart upload (`multipart/form-data`, single `file` field; the dataset name
comes from the part's filename) — matching the AMT-DTP ISE/ISCP service
convention. The server streams the part to a temp file in 1 MB chunks,
validates the name (rules above; extension must be `.jsonl`), then
**atomically moves** it into `datasets/` — a partially-uploaded file is never
visible to Play/Capture. `201` with the new dataset's metadata (the same shape
as a list entry) on success.

**Overwriting is not allowed** (deliberate deviation from the reference, which
replaces existing targets silently — datasets are irreplaceable recordings):
uploading onto an existing name is `409 dataset_exists`; to replace a dataset,
delete it first (itself guarded by `dataset_in_use`).

Errors: `415 unsupported_type` (extension/MIME is not a `.jsonl` upload),
`409 dataset_exists`, `409 dataset_in_use`, `413 too_large`,
`422 invalid_name`. (If a request model is ever needed alongside the file, use
the reference's `Json[Model]` form field + `openapi_extra` multipart-schema
helper.)

### `GET /api/v1/datasets/{name}` → metadata (same shape as list entry)
### `GET /api/v1/datasets/{name}/content` → streamed download
### `PATCH /api/v1/datasets/{name}`
```json
{"new_name": "renamed.jsonl"}
```
`204`; errors `409 dataset_in_use`, `409 dataset_exists`, `422 invalid_name`.

### `DELETE /api/v1/datasets/{name}` → `204`; `409 dataset_in_use`

### `GET /api/v1/datasets/{name}/devices`

Devices from the dataset's probe frame (for the Play launch form).

```json
{"devices": [{"id": "d1", "name": "Mazak", "uuid": "m80-uuid"}, {"id": "agent-id", "name": "Agent", "uuid": "…"}]}
```

Returns **all** devices; filtering out `Agent` stays a frontend concern.
`422 invalid_dataset` when the header or first frame isn't a valid probe.

## 7. Scope-specific configs

### Capture — `POST /api/v1/capture/sessions`

```json
{
  "base_url": "https://demo.mtconnect.org/",
  "mode": "stream",
  "interval_ms": 1000,
  "timeout_s": 30.0,
  "chunk_size": 4096,
  "filename": null
}
```

- `mode`: `stream` | `sample` | `current`.
- `filename: null` → **server generates it** (same scheme as today:
  sanitized base_url + UTC timestamp, collision-checked against files *and*
  in-flight captures). Echoed in the session envelope's `config.filename`.

### Play — `POST /api/v1/play/sessions`

```json
{
  "dataset": "demo.jsonl",
  "devices": [
    {"device_name": "Mazak", "shdr_host": "127.0.0.1", "shdr_port": 7878}
  ],
  "agent": {
    "port": 5000,
    "mtconnect_version": "2.7",
    "asset_buffer_size": 1024,
    "buffer_size": 131072
  },
  "re_timestamp": true,
  "loop": true
}
```

Validation (all-or-nothing, under the manager lock): dataset exists
(`404`), dataset not being captured (`409 dataset_in_use`), ≥1 device
(`422`), all ports free and mutually distinct (`409 port_conflict` with the
full conflict list, mirroring today's inline messages).

#### Playback control (running sessions)

| Endpoint | Effect |
|---|---|
| `GET /api/v1/play/sessions/{id}/progress` | Scrub-bar origin: `duration`, `pass_start_epoch`, `looping`, `paused`, `position`, `server_time`. The UI computes the live playhead as `server_time − pass_start_epoch`; while `paused` is true it holds the playhead at `position` instead (the origin stops advancing). |
| `POST /api/v1/play/sessions/{id}/seek` | Body `{"position": <sec>}`. Seek a running playback. Requires re-timestamp mode (`409 seek_unavailable` otherwise); `404`/`409 not_running` as applicable. `204`. |
| `POST /api/v1/play/sessions/{id}/pause` | Freeze the playhead target (stops it advancing). Works in **any** timestamp mode — unlike seek it does not rewind or re-order data. `409 not_running` if not running. `204`. |
| `POST /api/v1/play/sessions/{id}/resume` | Resume a paused playback. `409 not_running` if not running. `204`. |

Pause/resume ride the same worker command channel as seek. The player models a
**playhead target** (advances at 1× wall-clock while playing, frozen while paused,
set directly by a seek) separate from the **emit cursor** (how far data has been
released): frames emit while their time ≤ target — unpaced when behind (a post-seek
catch-up), paced when caught up. So **pause never blocks the emit loop** — a
post-seek fast-forward runs to the (frozen) target and then idles. The emit-timestamp
clock is kept separate and monotonic so a backward seek/loop-wrap never emits a
decreasing timestamp.

**Seek uses a keyframe index.** `snapshot_interval_s` on `PlayCreate` (default `15`,
fixed once playing) sets how often — in dataset seconds — a full-state snapshot
(a `/current` equivalent, folded from the recording) is indexed at load, together
with the byte offset of the frame that follows it. A seek jumps to the nearest
snapshot ≤ the target, emits that state, and replays only the ≤ `snapshot_interval_s`
of deltas up to the target — instead of replaying from the start of the recording.
Smaller intervals mean faster seeks but more memory and a longer one-time pre-scan.

### Simulate

Session **creation is one endpoint per equipment type** — each with a
dedicated pydantic body model so every equipment parameter set is fully
validated by its own schema (amends decision #10; see decision log #12):

| Endpoint | Body model |
|---|---|
| `POST /api/v1/simulate/start/mill_3axis` | `Mill3AxisCreate` |
| `POST /api/v1/simulate/start/mill_5axis_trunnion_bc` | `Mill5AxisTrunnionBcCreate` |
| `POST /api/v1/simulate/start/robot_6dof` | `Robot6DofCreate` |

The lifecycle endpoints (`GET/DELETE /simulate/sessions[...]`, `/routine`)
are shared across types; the session envelope's `config` echo is the
discriminated union of the create models (`equipment_type` is the
discriminator — a defaulted constant, so requests never need to send it).

**Axis limits.** Every equipment parameter is optional. Linear axes are
**meters**, rotary axes/joints are **degrees**, each a `{"min", "max"}` object
validated `max > min`. Defaults are the equipment's **native travel** (0–1 m
linear; per-axis rotary ranges), so `{}` reproduces the canned motion
identically. Configured limits **scale the routine** (affine, native →
configured, per axis), the 3D scene's geometry/workpiece, and are published as
MTConnect `Constraints` (`<Minimum>`/`<Maximum>`) on the axis DataItems in the
generated device model. The trunnion's C axis is continuous and has no limits.
Equipment keys are lowercase with axis orientations encoded where they matter
(`_bc`), anticipating future variants.

#### `GET /api/v1/simulate/equipment` — catalog for the launch form

```json
[
  {
    "type": "mill_3axis",
    "label": "3-Axis Mill",
    "default_device_name": "Mill",
    "routines": ["Default Routine"],
    "create_path": "/api/v1/simulate/start/mill_3axis"
  },
  {"type": "mill_5axis_trunnion_bc", "label": "5-Axis Trunnion Mill (B/C)", "default_device_name": "TrunnionMill", "routines": ["Default Routine"], "create_path": "/api/v1/simulate/start/mill_5axis_trunnion_bc"},
  {"type": "robot_6dof", "label": "6-DOF Robot", "default_device_name": "Robot", "routines": ["Default Routine"], "create_path": "/api/v1/simulate/start/robot_6dof"}
]
```

Each `create_path`'s request schema is that operation's body in
`/api/openapi.json` — generic clients can build launch forms without
hardcoding equipment knowledge.

#### `POST /api/v1/simulate/start/mill_5axis_trunnion_bc` (example)

```json
{
  "device_name": "DMU50",
  "routine": "Default Routine",
  "interval_ms": 10,
  "agent": {"port": 5000, "mtconnect_version": "2.7", "asset_buffer_size": 1024, "buffer_size": 131072},
  "shdr": {"host": "127.0.0.1", "port": 7878},
  "x": {"min": 0.0, "max": 0.5},
  "y": {"min": 0.0, "max": 0.5},
  "z": {"min": 0.0, "max": 0.5},
  "b": {"min": -110.0, "max": 30.0}
}
```

Every field is optional (`{}` is a valid body). The robot's model takes
`j1`…`j6` limit objects (degrees) instead of `x`/`y`/`z`/`b`; its link lengths
are fixed physical structure, not parameters.

#### `GET /api/v1/simulate/sessions/{id}/routine` — feeds the 3D view

```json
{
  "spec": { "…RoutineSpec.to_dict()…": "equipment, coord_names, start, cycle_time, geometry, moves" },
  "start_epoch": 1753293851.204,
  "server_time": "2026-07-23T18:04:15.732Z"
}
```

- `start_epoch` is `null` until the session reaches `running` (no data
  timeline to phase-lock to yet — the UI keeps its current "reopen in a
  moment" behavior).
- `server_time` lets the client compute a clock offset once
  (`offset = Date.now() − server_time`), making the phase-lock correct even if
  a future remote-UI mode ships. Costs nothing now.

## 8. Events & logs (NDJSON)

One **global event bus**: every event gets a **globally monotonic `seq`** and a
**`ts`** (both mandatory on *every* line, all event types). Events are held in
a bounded ring buffer; `scope` / `session` / `level` are *filters*, not
separate counters — so any filter combination works with a single cursor.

**Default (accepted):** ring buffer capacity **10,000 events**, memory-only
(no disk persistence). This also bounds what today is an unbounded
`log_lines` list.

### Event records

```jsonl
{"type":"log","seq":10423,"ts":"2026-07-23T18:04:11.234Z","scope":"play","session":"play-3","level":"INFO","logger":"src.converter","msg":"playback started"}
{"type":"session","seq":10424,"ts":"2026-07-23T18:04:11.240Z","scope":"play","session":"play-3","event":"status","status":"running"}
{"type":"session","seq":10425,"ts":"2026-07-23T18:05:02.001Z","scope":"simulate","session":"sim-2","event":"status","status":"running","detail":{"start_epoch":1753293851.204}}
{"type":"ping","seq":10425,"ts":"2026-07-23T18:05:17.000Z"}
```

- `log` — a backend log record attributed to a session (the `\x1f`
  thread-name mechanism becomes an internal detail of the ingest path).
- `session` — lifecycle transitions (`status` events on every state change),
  so a streaming client never needs to poll for status.
- `ping` — heartbeat every **15 s** of stream idle time, so proxies/clients
  can distinguish "quiet" from "dead".
- **Resolved — ping `seq` semantics:** pings satisfy the "seq on every
  line" rule by carrying the **last delivered event's seq** (a cursor
  checkpoint: "you are current through N"). They are *not* stored in the
  buffer and do *not* increment the counter — otherwise backfills would replay
  stale pings, and cursor gaps would be indistinguishable from missed events.
- **Integrity contract:** within a connection, TCP already guarantees ordering
  and delivery — `seq` exists for *cross-reconnect* continuity (the `after`
  cursor) and buffer-overrun detection (`dropped`). Because the counter is
  global, a *filtered* view legitimately skips values, so "seq increments by
  exactly 1" is **not** a valid gap check on filtered streams.
- Clients must ignore unknown `type`s and unknown fields (forward
  compatibility).

- **Filter semantics:** `scope` and `session` are exact matches. `level` is a
  **minimum severity** (`level=INFO` includes `WARNING`/`ERROR`) and only
  constrains `log` events — `session` status events always pass a level filter.
- Because the counter is global, a *filtered* poll's `next` cursor may skip
  seq values that belonged to filtered-out events; this is by design (they can
  never match the same filter later).

### `GET /api/v1/events?scope=play&session=play-3&level=INFO&after=10400&limit=500`

Poll mode. All filters optional; `after` is exclusive; `limit` default 500.

```json
{
  "events": [ { "…": "…" } ],
  "next": 10424,
  "dropped": false
}
```

`dropped: true` signals the cursor fell off the ring buffer's tail (client
should render a gap marker rather than assume continuity).

### `GET /api/v1/events/stream?scope=play&after=10400`

Streaming mode: `application/x-ndjson`, chunked. Backfills everything after
the cursor from the buffer, then stays live. Same filters as poll mode.
Reconnect = re-request with your last seen `seq`.

> **Send `Accept-Encoding: identity` on stream requests.** A gzip layer (the
> app's own middleware stack, or any proxy) buffers compressed output, holding
> lines back indefinitely on a quiet stream. The Python client library does
> this automatically; hand-rolled clients (`curl` defaults to identity) must
> not request compression here.

`/events/stream?scope=play` **is** the per-service notification stream:
session-status events ride alongside logs on the same connection, so a client
subscribed to a scope learns of completion/errors without polling.

*(The Streamlit frontend runs one shared background reader per scope on the
stream endpoint — logs and status events feed queues that the rerun/fragment
loops drain, exactly like today's log-queue pattern. Poll mode serves simple
request/response clients and reconnect backfill.)*

## 9. Schemas & client library

Shared pydantic models live in the **client library component** (`lib/` at the
repo root) — the reference pattern, where the client lib is also the schema
home. The app installs it into its venv (editable in dev; built wheel for
release builds) and the FastAPI routes, services layer, and the Streamlit
frontend's API client all import the same classes. FastAPI's OpenAPI (at
`/api/openapi.json`, docs at `/api/docs`) is generated from these and is the
contract any future frontend builds against.

```
lib/
  pyproject.toml                    # wheel: mtconnect-explorer-client (version lockstep with the app)
  src/mtconnect_explorer_client/
    __init__.py                     # ApiClient + schema re-exports
    client.py                       # sync httpx ApiClient — flat per-endpoint methods (reference style)
    events.py                       # events_stream() NDJSON iterator
    exceptions.py                   # typed exceptions mapped from error codes
    schemas/
      __init__.py
      common.py                     # ErrorDetail, SessionInfo, SessionStatus, PortsInfo
      capture.py                    # CaptureCreate, CaptureConfig
      play.py                       # PlayCreate, PlayDevice, AgentSettings
      simulate.py                   # SimulateCreate, ShdrSettings, EquipmentInfo, RoutineInfo
      datasets.py                   # DatasetInfo, RenameRequest, DeviceInfo
      events.py                     # LogEvent, SessionEvent, PingEvent, EventPage
      system.py                     # VersionInfo, AgentInfo
```

## 10. Non-normative implementation notes

*(Internal architecture — recorded here so the API's behavior guarantees have
a stated basis; not part of the wire contract.)*

- Routes are thin wrappers over `src/services/` managers; all session mutation
  happens under the manager lock (atomic validate-and-reserve).
- Workers run as **threads in v1** behind a host seam (`ThreadHost`), written
  subprocess-ready: a worker is a pure function of `(config, event_sink)`. A
  later `SubprocessHost` runs the same worker via self-exec
  (`MTConnectExplorer --worker <scope>`, config on stdin, events as NDJSON on
  stdout) — flipping a scope between hosts changes no API behavior.
- The hot data path (SHDR emission → agent → HTTP consumers; collector →
  dataset file) lives entirely inside a worker and never crosses the API.
- The Streamlit frontend authenticates with the runtime token; its API base
  URL and key are injected config, so pointing it at a remote backend later is
  a flag, not a refactor.

## 11. Decision log

| # | Item | Status |
|---|---|---|
| 1 | Refuse to start when exposed (`--host` non-local) without `api_key` | ✅ Confirmed (§2) |
| 2 | `/health` unauthenticated | ✅ Confirmed (§2) |
| 3 | Terminal-session retention | ✅ Confirmed — event bus is the primary signal; retention is a REST safety net (§5) |
| 4 | Upload style | ✅ Resolved — multipart `POST /datasets/upload` per the AMT-DTP ISE/ISCP service convention; overwriting existing datasets is not allowed (§6) |
| 5 | Event ring buffer capacity / persistence | ✅ Accepted by default — 10,000, memory-only (§8) |
| 6 | Ping `seq` = checkpoint of last event, not stored | ✅ Confirmed (delegated) (§8) |
| 7 | Heartbeat interval 15 s | ✅ Accepted by default (§8) |
| 8 | One Swagger tag per service | ✅ Confirmed (§1) |
| 9 | Error shape | ✅ Resolved — typed object inside FastAPI-native `detail` (§3) |
| 10 | URL grammar | ✅ Resolved — resource-REST paths kept; deliberate deviation from the reference's RPC style (§1) |
| 11 | Client library | ✅ Resolved — `lib/` component at repo root is the schema home + sync httpx client; wheel `mtconnect-explorer-client`, version lockstep with the app (§9) |
| 12 | Per-equipment simulate creation | ✅ Resolved (2026-07-24) — `POST /simulate/start/{equipment_type}`, one dedicated body model per type (a deliberate RPC-style amendment to #10, chosen so each equipment's parameters are validated by its own schema as simulator types multiply). Axis limits: meters/degrees, `max > min`, defaults = native travel, limits **scale** the routine + 3D scene + published MTConnect Constraints. The generic `POST /simulate/sessions` was **removed** (not deprecated — pre-release). Equipment keys lowercase w/ axis orientation suffixes (`mill_5axis_trunnion_bc`); `tick_ms` renamed `interval_ms`; all routines named "Default Routine" (§7) |
