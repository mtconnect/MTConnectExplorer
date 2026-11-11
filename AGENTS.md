# AGENTS.md

Guidance for AI coding agents and humans working in this repository. It is the single source of
project instructions: coding tools that support the AGENTS.md convention read it directly, and
Claude Code loads it through the `@AGENTS.md` import in [CLAUDE.md](CLAUDE.md).

MTConnectExplorer is an early-stage app for exploring, capturing, replaying, and simulating
[MTConnect](https://github.com/mtconnect/cppagent) machine-tool data, implementing the Testbed as a
Service (TaaS) capture/replay method (see [README.md](README.md)). It is a **Streamlit frontend +
FastAPI backend in one process, on one port** (UI at `/`, API at `/api/v1`, Swagger at `/api/docs`),
plus a Python client wheel.

## Privacy and secrets (mandatory)

**Treat this repository as public.** Everything committed is published and stays in git history
after deletion: file contents, filenames, images, commit messages, PR text, and commit
author/committer identities. A later commit that removes a leak does **not** remove it from history,
forks, or caches.

### Keep out of the repository (and what to use instead)

1. **Absolute paths**: `<drive>:\Users\<name>\…`, `/home/<name>/…`, `/Users/<name>/…`,
   `file:///<drive>:/…`, UNC shares (`\\<host>\<share>`), or the expanded value of `~`,
   `%USERPROFILE%`, `$HOME`, or a temp dir. Use repo-relative paths, `${workspaceFolder}`,
   `Path(__file__).parent`, `tempfile`, or leave `~` / environment variables unexpanded.
2. **Usernames, logins, and machine identity**: OS login names, hostnames, Windows domains, and
   personal account handles in code, config, examples, test fixtures, or log excerpts. Write
   placeholders such as `<user>` and `<host>` instead.
3. **Email addresses**: in examples use the reserved `example.com` / `example.org` domains
   ([RFC 2606](https://www.rfc-editor.org/rfc/rfc2606)).
4. **Credentials**: API keys, tokens, passwords, private keys and certificates (`*.pem`, `*.key`,
   `*.p12`, `*.pfx`), `.env` files, cookies, signed URLs, connection strings with passwords. This
   includes "temporary", "local-only", and "test-against-prod" values. Docs use obvious placeholders
   (`<your-api-key>`); tests use obviously fake values (`test-api-key`).
5. **Private network details**: internal IPs and hostnames, customer, site, or plant names, and
   machine serial numbers. Allowed: `127.0.0.1`, `::1`, `localhost`, `0.0.0.0`, the documentation
   ranges ([RFC 5737](https://www.rfc-editor.org/rfc/rfc5737)), and public demo agents such as
   `demo.mtconnect.org`.
6. **Captured data**: `datasets/` and `test/` are gitignored; keep them that way. MTConnect data is
   full of identity even when it looks like telemetry:
   - every response `<Header sender="…">` is the **agent host's computer name** (a capture of a local
     agent records *this* machine's hostname; `--loopback-test` output does too);
   - device models carry `serialNumber`s, UUIDs, and equipment IPs and hostnames;
   - Capture names the file after the `base_url` (`http_<ip>_<port>-<timestamp>.jsonl`), so the
     filename alone leaks the agent's address;
   - **synthetic datasets inherit all of this** when they are built from a real capture's probe
     frame.

   Before a dataset leaves this machine, set every `sender` to a neutral value (`synthetic`),
   replace serial numbers and UUIDs, and rename the file. If fixtures are ever committed, generate
   them synthetically or capture a public demo agent.
7. **Credentials in URLs**: use agent URLs without credentials (`http://<host>:<port>/`, with no
   `user:password@` and no `?token=…`). Capture copies the URL into the dataset filename, every
   recorded frame's `url`, the session label, and the debug log.
8. **Local state**: `config.json`, `.claude/*.local.json`, `.env*`, IDE per-user state, logs, and
   crash dumps are gitignored. Keep them local; to share a setting, commit a redacted example
   (for example `config.example.json`) instead.

### Where leaks hide (check these too, not just source)

- **Generated files**: `pip freeze` embeds `file:///…` paths for editable installs (use
  `--exclude`; see Commands below), and the same goes for `*.egg-info`, build specs, and test
  output.
- **Binaries built on your machine**: every `.pyc` records the absolute path of the source it was
  compiled from. `installer.spec` therefore bundles `src/` file by file and skips `__pycache__/`
  and `*.pyc`; keep it that way, because a build that bundles them embeds your home path and
  login. Publish CI-built release artifacts; before sharing any local build, scan it for your home
  path (PyInstaller's `CArchiveReader` can list and extract every bundled file).
- **Logs and tracebacks**: the bundled agent logs the absolute path of its generated config, and
  Python tracebacks show absolute paths; terminal prompts show `user@host`. Trim to repo-relative
  paths before pasting into docs, issues, or PRs.
- **Images**: before committing a screenshot, check the title bar, address bar, file dialogs,
  account avatars, notifications, and tokens in URLs. Strip metadata (PNG text chunks, EXIF).
- **Commit metadata**: author and committer email must be a no-reply address (for GitHub, the
  `<id>+<handle>@users.noreply.github.com` form with "Keep my email addresses private" enabled).
  Commit messages and PR descriptions follow the same rules as files. Two things git always
  publishes and that are accepted here: the noreply address reveals the GitHub handle and numeric
  account ID, and every commit date carries the committer's timezone offset.

### Allowed: public attribution and public links

- **People's names and personal account handles** only in `README.md`, `LICENSE.TXT`, and
  `NOTICE`, as deliberate attribution: author names in paper citations, the TaaS repository links,
  and copyright holders. Code, config, examples, and tests use neutral placeholders instead.
- **Links to public organization or project repositories and published documents** (for example
  `mtconnect/cppagent`, or `ManufacturingTechnology/DTP-Isaac-Sim-Service` in docs/api.md) are fine
  anywhere, but **confirm the target is public before linking or naming it**
  (`gh repo view <owner>/<repo> --json visibility`, or an anonymous `git ls-remote`). The name of a
  private or internal project is internal information even when it is not a secret.

### Moving or sharing the project

Create any new copy **from git** (push to the new remote, clone, or `git archive`), never by copying
the working folder. The folder holds gitignored files that never belong in a repository: the
`config.json` key, `.claude/settings.local.json` (home path and login), `datasets/` (real hostnames,
private IPs, serial numbers), `test/` outputs (this machine's hostname), `__pycache__/` (home
path), `dist/` (local builds; ones made before the spec fix embed the home path), `venv/`, and
`.git/` itself (reflog, `FETCH_HEAD` with the old remote name).

### Enforcement

- `python -m pytest tests/test_privacy.py` scans every tracked and unignored file for absolute
  paths, email addresses, credential patterns (token formats, private keys, passwords in URLs, secret
  literals), image metadata, and this machine's home directory, login, hostname, and git identity, and
  checks that the sensitive local paths above stay gitignored. Matches are redacted in its output. CI runs it
  with the rest of the suite. **Run it before every commit.**
- The same patterns (in [tests/privacy_rules.py](tests/privacy_rules.py), shared with
  `tests/test_agent_docs.py`) run in [.github/scripts/check-content.py](.github/scripts/check-content.py)
  at two earlier points: as a Claude Code PreToolUse hook ([.claude/settings.json](.claude/settings.json))
  that blocks a Write or Edit putting a match into a tracked file, and as an opt-in pre-commit hook
  that scans the staged files (enable it once per clone: `git config core.hooksPath .githooks`).
  `python .github/scripts/check-content.py` alone scans the whole repository. The write hook fails
  closed: if the check cannot run (no `python3` on `PATH`, or the script errors), every write is
  blocked. Install Python 3 as `python3` rather than removing the hook.
- A false positive goes in `ALLOWED` (or `ALLOWED_TRANSCRIPT` for the recording rules) in
  `tests/privacy_rules.py`, with a one-line reason. Never weaken a pattern to make a hit go away.
- When you change a pattern in `tests/privacy_rules.py`, add a sample to
  `test_patterns_catch_known_leaks` for the case that prompted the change, and keep every existing
  sample passing. To silence a duplicate or a false positive, add a skip in `_violations` or an
  `ALLOWED` entry rather than narrowing the regex, because a narrower regex silently stops catching
  real leaks. `test_every_check_has_leak_samples` fails when a check has no sample, or when the
  email check lacks one at each of its boundary forms.
- Review `git diff --cached` against this list before committing.
- **If a secret is committed or pushed, treat it as compromised.** Revoke or rotate it first, then
  remove it. Rewriting published history is destructive and needs the maintainer's explicit
  go-ahead.

### Rules specific to AI agents

- Where your own environment's values would go (repo files, commit messages, PR descriptions,
  issue comments), write placeholders (`<home>`, `<user>`, `<host>`) and repo-relative paths.
  That covers your working directory, username, home path, hostname, environment variables,
  `config.json` contents, and tool output that contains them.
- In committed docs, cite code by repo-relative path (`src/services/auth.py:20`), never by
  absolute path.
- When checking a secret (for example the `config.json` `api_key`), report whether it is present,
  its length, or whether it matches a known value. Never print the value itself.
- When generating or transforming datasets, write neutral identity (`sender="synthetic"`,
  placeholder serials and UUIDs) rather than copying it from a source capture.
- Before naming or linking another project, confirm it is public (see above). If you can't, ask.

## Recording knowledge

Files in this repository hold instructions that help the next agent do the right thing; they are
not a record of conversations. These rules apply to everything written into the repository (this
file, CLAUDE.md, docs, code comments, commit messages, PR text) and to any status register, ledger,
or decisions log added later.

1. **Write instructions in your own words.** Turn each piece of guidance into a directive: *when
   [situation], do [behavior], because [reason]*, specific enough to act on and to check. Store the
   instruction itself, never the wording that prompted it, whether or not it is quoted.
2. **Describe the behavior to perform.** Keep prohibitions for hard limits (safety, data loss,
   irreversible actions, legal or licensing constraints, and the privacy rules above), and pair each
   one with what to do instead.
3. **Clarify before recording.** When guidance names a problem rather than a behavior (a correction,
   a complaint, something vague), ask follow-up questions until you can state the behavior that
   would satisfy it, then record that behavior. If you can't ask, record your best reading, mark it
   **unconfirmed**, and raise it as an open question.
4. **File it where it applies.**
   - Rules for working on the project → this file (CLAUDE.md imports it; Claude Code-specific
     notes go below the import there), or the doc for that subsystem.
   - API and architecture decisions → the decision log in [docs/api.md](docs/api.md) §11, with the
     date and the rationale.
   - Bugs → an issue on the project's GitHub repository: the observable symptom plus reproduction
     steps.
   - Personal working preferences (pace, tone, level of detail) → the agent's private memory or
     user-level configuration, outside the repository.
   - If the project adds a status register, ledger, or project-wide decisions log, file the matching
     items there, under these same rules.
5. **Record the rule, not the story.** Write what to do and why. Leave out who said what, how anyone
   felt, and anyone's workload. Where provenance matters, `user decision, YYYY-MM-DD` is enough.
6. **Quote data freely.** UI labels, error messages, standards, license text, and tool output can be
   quoted as they are, with personal paths and identities redacted per the privacy rules.

**Example.** Feedback says the agent keeps reversing itself. Follow-up questions establish that fixes
keep breaking neighboring features. The recorded instruction: *"After each fix, run the full test
suite and the checks for neighboring features before reporting the work done; when a fix touches
shared code, list the other features it affects."* The feedback itself is recorded nowhere.

[tests/test_agent_docs.py](tests/test_agent_docs.py) (and the write hook above) fails when CLAUDE.md, AGENTS.md,
contributing.md, or `docs/` attribute guidance to a person or quote it in the first person; restate
the hit as an instruction.

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

- Import `streamlit` only in the frontend (`src/app.py`, `src/pages/`) and in `src/main.py`'s
  server start-up; `src/services/` and `src/api/` stay UI-independent so workers and the API tests
  run without Streamlit.
- `lib/` depends only on its declared packages (pydantic, httpx); dependencies point from `src/` to
  `lib/`: services/api/pages all import schemas from `mtconnect_explorer_client.schemas` — the
  single source of truth for the wire contract.
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
- `config.json`, `.claude/*.local.json` (any `*.local.json`), `.env*`, key/cert files, `*.log`,
  `datasets/`, `dist/`, `build/`, `src/bin/`, `test/` (but **not** `tests/`), `venv/`, and
  `working/` (local drafts, such as issue texts waiting to be filed) are gitignored. `CLAUDE.md`, `AGENTS.md`, and `.vscode/` **are** tracked and published, so they fall
  under the privacy rules above.
- Both build entry points use the same [installer.spec](installer.spec) (PyInstaller), which bundles
  all of `src/` and reads `__version__` to stamp Windows file-version fields.
