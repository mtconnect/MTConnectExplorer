"""Tests for the Play scrub/seek feature — player mechanics, manager validation,
and an agent-gated end-to-end lifecycle."""

import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from mtconnect_explorer_client import ApiError
from mtconnect_explorer_client.schemas import PlayCreate, PlayDevice, SessionPorts

from src.converter import MtConnectDatasetPlayer, dataset_duration
from src.services.errors import Conflict, NotFound
from src.services.play import play_manager
from src.services.sessions import STATE_LOCK, Session

from .conftest import API_KEY, requires_agent

REPO_ROOT = Path(__file__).parent.parent


# --- dataset_duration (pure file parse; no agent) --------------------------
def test_dataset_duration(tmp_path):
    # duration = last frame TS - first OBSERVATION frame TS (probe line skipped).
    ds = tmp_path / "d.jsonl"
    ds.write_text(
        "RawMTConnect-v0.1\n"
        '{"TIMESTAMP": "2026-07-24T00:00:00.000+00:00", "ID": "p", "PAYLOAD": {}}\n'   # probe
        '{"TIMESTAMP": "2026-07-24T00:00:05.000+00:00", "ID": "o", "PAYLOAD": {}}\n'   # obs start
        '{"TIMESTAMP": "2026-07-24T00:00:35.000+00:00", "ID": "o", "PAYLOAD": {}}\n'   # obs end
    )
    assert dataset_duration(str(ds)) == pytest.approx(30.0)


def test_dataset_duration_probe_only_is_zero(tmp_path):
    ds = tmp_path / "d.jsonl"
    ds.write_text(
        "RawMTConnect-v0.1\n"
        '{"TIMESTAMP": "2026-07-24T00:00:00.000+00:00", "ID": "p", "PAYLOAD": {}}\n'
    )
    assert dataset_duration(str(ds)) == 0.0


# --- playhead / emit-clock invariants (unit on the player object; no I/O) ---
def test_emit_ts_is_strictly_increasing_across_backward_jump():
    # The emit-timestamp clock must never decrease, even when a seek jumps the
    # playhead backward (the agent rejects decreasing timestamps).
    p = MtConnectDatasetPlayer(re_timestamp=True)
    p._emit_origin = datetime.now(timezone.utc)
    p._elapsed_offset = timedelta(0)
    p._last_emit_ts = None
    t_fwd = p._emit_ts(50.0)   # emit at offset 50s
    t_back = p._emit_ts(10.0)  # backward jump to 10s — still must increase
    assert t_back > t_fwd


def test_pause_freezes_playhead_and_resume_advances():
    # Pause freezes the playhead target; resume lets it advance again.
    p = MtConnectDatasetPlayer(re_timestamp=True)
    p._duration = 100.0
    p._wall_anchor = datetime.now(timezone.utc) - timedelta(seconds=20)
    p._pos_anchor = 0.0
    p._paused = False
    pos_before = p.position
    assert pos_before == pytest.approx(20.0, abs=1.0)

    p.pause()
    assert p.is_paused
    frozen = p.position
    time.sleep(0.2)
    assert p.position == pytest.approx(frozen, abs=0.05)  # held while paused

    p.resume()
    assert not p.is_paused
    time.sleep(0.2)
    assert p.position > frozen  # advancing again


def test_seek_clamps_to_duration():
    p = MtConnectDatasetPlayer(re_timestamp=True)
    p._duration = 42.0
    p.seek(999.0)     # beyond the end
    assert p._seek_target == pytest.approx(42.0)
    p.seek(-5.0)      # before the start
    assert p._seek_target == pytest.approx(0.0)


# --- manager validation (unit; inject a fake session, no worker) -----------
def _inject_session(sid: str, status: str, re_timestamp: bool) -> Session:
    create = PlayCreate(
        dataset="x.jsonl",
        devices=[PlayDevice(device_name="D")],
        re_timestamp=re_timestamp,
    )
    sess = Session(id=sid, scope="play", label="x", config=create, ports=SessionPorts())
    sess.status = status
    with STATE_LOCK:
        play_manager.sessions[sid] = sess
    return sess


def _drop_session(sid: str) -> None:
    with STATE_LOCK:
        play_manager.sessions.pop(sid, None)


def test_seek_unknown_session_raises_not_found():
    with pytest.raises(NotFound) as exc:
        play_manager.seek("play-does-not-exist", 5.0)
    assert exc.value.code == "session_not_found"


def test_seek_not_running_is_conflict():
    sid = "play-test-notrunning"
    _inject_session(sid, status="starting", re_timestamp=True)
    try:
        with pytest.raises(Conflict) as exc:
            play_manager.seek(sid, 5.0)
        assert exc.value.code == "not_running"
    finally:
        _drop_session(sid)


def test_seek_without_re_timestamp_is_conflict():
    sid = "play-test-nortts"
    _inject_session(sid, status="running", re_timestamp=False)
    try:
        with pytest.raises(Conflict) as exc:
            play_manager.seek(sid, 5.0)
        assert exc.value.code == "seek_unavailable"
    finally:
        _drop_session(sid)


def test_seek_running_re_timestamp_ok_no_handle():
    # A valid seek on a running re-timestamp session with no worker handle is a
    # no-op (validation passes; send_command skips the None handle) — proves the
    # validation gate, without needing a real worker.
    sid = "play-test-ok"
    _inject_session(sid, status="running", re_timestamp=True)
    try:
        play_manager.seek(sid, 5.0)  # must not raise
    finally:
        _drop_session(sid)


# --- API validation (via the client) ---------------------------------------
def test_seek_unknown_session_404_over_api(client):
    with pytest.raises(ApiError) as exc:
        client.play_seek("play-99999", 1.0)
    assert exc.value.status_code == 404
    assert exc.value.code == "session_not_found"


def test_seek_negative_position_rejected_by_schema(server):
    # PlaySeek.position has ge=0.0 — a negative seek fails schema validation (422)
    # before reaching the manager.
    response = httpx.post(
        f"{server}/v1/play/sessions/play-1/seek",
        json={"position": -1.0},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 422


# --- end-to-end (needs the bundled agent + a real RawMTConnect dataset) -----
def _real_dataset() -> Path | None:
    ds_dir = REPO_ROOT / "datasets"
    if not ds_dir.is_dir():
        return None
    files = sorted(ds_dir.glob("*.jsonl"), key=lambda f: f.stat().st_size)
    return files[0] if files else None


@requires_agent
def test_play_seek_lifecycle(client):
    """Start a looping playback, read /progress, seek forward and backward, and
    confirm the reported origin rebases (the playhead actually moves)."""
    src_ds = _real_dataset()
    if src_ds is None:
        pytest.skip("no real RawMTConnect dataset available to replay")

    from src.services import datasets as datasets_service
    shutil.copy(src_ds, datasets_service.ensure_dir() / src_ds.name)

    devices = [d for d in client.datasets_devices(src_ds.name).devices if d.name != "Agent"]
    if not devices:
        pytest.skip("dataset has no replayable device")

    p = client.ports(shdr_count=1)
    sess = client.play_start(PlayCreate(
        dataset=src_ds.name,
        devices=[PlayDevice(device_name=devices[0].name, shdr_port=p.suggested.shdr[0])],
        agent={"port": p.suggested.agent},
        loop=True,
        re_timestamp=True,
    ))
    try:
        # Wait for running.
        deadline = time.time() + 90
        while client.play_get(sess.id).status != "running":
            assert time.time() < deadline, "playback never reached running"
            time.sleep(1.0)

        prog = client.play_progress(sess.id)
        assert prog.duration > 0
        assert prog.pass_start_epoch is not None
        assert prog.looping is True

        def position() -> float:
            pr = client.play_progress(sess.id)
            return pr.server_time.timestamp() - pr.pass_start_epoch

        # Seek to ~75% and confirm the playhead lands near there.
        target = prog.duration * 0.75
        client.play_seek(sess.id, target)
        deadline = time.time() + 15
        while abs((position() % prog.duration) - target) > max(3.0, prog.duration * 0.05):
            assert time.time() < deadline, "forward seek did not take"
            time.sleep(0.5)

        # Seek backward to ~10% and confirm it moves back.
        target_back = prog.duration * 0.10
        client.play_seek(sess.id, target_back)
        deadline = time.time() + 15
        while abs((position() % prog.duration) - target_back) > max(3.0, prog.duration * 0.05):
            assert time.time() < deadline, "backward seek did not take"
            time.sleep(0.5)
    finally:
        client.play_stop(sess.id)
