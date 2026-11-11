from src.services.events import BUS, EventBus


def test_bus_seq_is_monotonic_and_cursorable():
    bus = EventBus(capacity=100)
    posted = [bus.post({"type": "log", "level": "INFO", "logger": "t", "msg": f"m{i}"})
              for i in range(5)]
    seqs = [e["seq"] for e in posted]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 5

    events, next_cursor, dropped = bus.query(after=seqs[1])
    assert [e["seq"] for e in events] == seqs[2:]
    assert next_cursor == seqs[-1]
    assert dropped is False


def test_bus_overrun_sets_dropped():
    bus = EventBus(capacity=3)
    for i in range(10):
        bus.post({"type": "log", "level": "INFO", "logger": "t", "msg": str(i)})
    events, _next, dropped = bus.query(after=2)
    assert dropped is True
    assert len(events) == 3  # only the ring's tail survives


def test_bus_filters():
    bus = EventBus(capacity=100)
    bus.post({"type": "log", "scope": "play", "session": "play-1",
              "level": "DEBUG", "logger": "t", "msg": "a"})
    bus.post({"type": "log", "scope": "simulate", "session": "sim-2",
              "level": "ERROR", "logger": "t", "msg": "b"})
    bus.post({"type": "session", "scope": "play", "session": "play-1",
              "event": "status", "status": "running"})

    by_scope, _, _ = bus.query(scope="play")
    assert [e["type"] for e in by_scope] == ["log", "session"]

    # level is a minimum severity and only constrains log events.
    by_level, _, _ = bus.query(level="INFO")
    assert [e.get("msg", e["type"]) for e in by_level] == ["b", "session"]

    by_session, _, _ = bus.query(session="sim-2")
    assert len(by_session) == 1


def test_poll_endpoint_returns_new_events(client):
    start = BUS.last_seq
    posted = BUS.post({"type": "log", "level": "INFO", "logger": "tests", "msg": "hello api"})
    page = client.events(after=start)
    seqs = [e.seq for e in page.events]
    assert posted["seq"] in seqs
    assert page.next >= posted["seq"]
    assert page.dropped is False


def test_stream_backfills_after_cursor(client):
    posted = BUS.post({"type": "log", "level": "INFO", "logger": "tests", "msg": "stream me"})
    stream = client.events_stream(after=posted["seq"] - 1, reconnect=False)
    first = next(stream)
    assert first.seq == posted["seq"]
    assert first.msg == "stream me"
    stream.close()
