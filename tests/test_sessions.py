import time

import httpx
import pytest

from mtconnect_explorer_client import ApiError

from .conftest import API_KEY, requires_agent


def test_capture_validation_422(server):
    # Raw request: the lib client validates Create models locally, so the
    # server-side 422 path needs a request that bypasses that validation.
    response = httpx.post(
        f"{server}/v1/capture/sessions",
        json={"base_url": ""},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)  # FastAPI-native format


def test_play_unknown_dataset_404(client):
    with pytest.raises(ApiError) as exc_info:
        client.play_start(
            dataset="does-not-exist.jsonl",
            devices=[{"device_name": "Mill", "shdr_port": 7878}],
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "dataset_not_found"


def test_play_requires_a_device(server):
    response = httpx.post(
        f"{server}/v1/play/sessions",
        json={"dataset": "x.jsonl", "devices": []},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 422


def test_simulate_unknown_equipment_is_404(server):
    # Unknown equipment types have no endpoint at all.
    response = httpx.post(
        f"{server}/v1/simulate/start/warp_drive",
        json={},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 404


def test_generic_simulate_create_was_removed(server):
    # POST /simulate/sessions was replaced by the per-type /simulate/start/*
    # endpoints; the path only supports GET/DELETE now.
    response = httpx.post(
        f"{server}/v1/simulate/sessions",
        json={"equipment_type": "mill_3axis"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 405


def test_simulate_axis_limits_are_validated(server):
    response = httpx.post(
        f"{server}/v1/simulate/start/mill_3axis",
        json={"x": {"min": 0.5, "max": 0.2}},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 422
    assert any("greater than min" in str(e.get("msg", "")) for e in response.json()["detail"])


def test_unknown_session_404(client):
    with pytest.raises(ApiError) as exc_info:
        client.simulate_get("sim-99999")
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "session_not_found"


def test_equipment_catalog(client):
    catalog = client.simulate_equipment()
    types = {e.type for e in catalog}
    assert {"mill_3axis", "mill_5axis_trunnion_bc", "robot_6dof"} <= types
    for entry in catalog:
        assert entry.label and entry.default_device_name
        assert entry.routines == ["Default Routine"]
        assert entry.create_path == f"/api/v1/simulate/start/{entry.type}"


@requires_agent
def test_simulate_full_lifecycle(client):
    """Start a real simulation (bundled agent) with configured axis limits,
    wait for running, read the scaled routine, prove the port conflict, then
    stop and verify teardown."""
    from mtconnect_explorer_client import AgentSettings, LinearAxisLimits, Mill3AxisCreate, ShdrSettings

    ports = client.ports(shdr_count=1)
    agent_port = ports.suggested.agent
    shdr_port = ports.suggested.shdr[0]

    sess = client.simulate_start(Mill3AxisCreate(
        device_name="TestMill",
        agent=AgentSettings(port=agent_port),
        shdr=ShdrSettings(port=shdr_port),
        x=LinearAxisLimits(min=0.0, max=0.5),   # meters — scales the routine
    ))
    try:
        assert sess.scope == "simulate"
        assert sess.status == "starting"
        assert sess.ports.agent == agent_port
        assert sess.ports.shdr == [shdr_port]
        assert sess.config.equipment_type == "mill_3axis"
        assert sess.config.routine == "Default Routine"
        assert sess.config.x.max == 0.5  # config echo carries the limits

        # Ports are held while the session is live.
        assert agent_port in client.ports().used

        # A second session on the same ports must be rejected atomically.
        with pytest.raises(ApiError) as exc_info:
            client.simulate_start(Mill3AxisCreate(
                device_name="Clash",
                agent=AgentSettings(port=agent_port),
                shdr=ShdrSettings(port=shdr_port),
            ))
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "port_conflict"
        conflicts = (exc_info.value.context or {}).get("conflicts", [])
        assert any(c["port"] == agent_port and c["held_by"] == sess.id for c in conflicts)

        # Agent boot + SHDR connect takes a while on Windows.
        deadline = time.time() + 90
        while True:
            current = client.simulate_get(sess.id)
            if current.status == "running":
                break
            assert current.status == "starting", f"unexpected status {current.status!r}"
            assert time.time() < deadline, "simulation never reached running"
            time.sleep(1.0)

        assert current.started_at is not None

        routine = client.simulate_routine(sess.id)
        assert routine.start_epoch is not None
        assert routine.spec.get("moves"), "routine spec should carry moves"
        # The routine and 3D geometry were scaled into the configured limits
        # (0.5 m = 500 mm in the simulator's native units).
        assert routine.spec["geometry"]["env"]["x1"] == 500.0
        assert all(m["target"][0] <= 500.0 + 1e-6 for m in routine.spec["moves"])

        # The session's agent is discoverable.
        assert any(sess.id in a.sessions for a in client.agents_list())

        # The event bus carried the running transition.
        page = client.events(scope="simulate", session=sess.id)
        statuses = [e.status for e in page.events if e.type == "session"]
        assert "starting" in statuses and "running" in statuses
    finally:
        client.simulate_stop(sess.id)

    with pytest.raises(ApiError):
        client.simulate_get(sess.id)  # removed by DELETE
    assert agent_port not in client.ports().used
