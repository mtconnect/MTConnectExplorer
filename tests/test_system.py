import httpx
import pytest

from mtconnect_explorer_client import ApiClient, ApiError

from src.__version__ import __version__


def test_health_is_unauthenticated(server):
    response = httpx.get(f"{server}/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_requests_without_key_are_401(server):
    response = httpx.get(f"{server}/v1/version")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"


def test_client_raises_typed_unauthorized(server):
    with ApiClient(f"{server}/v1") as anon:
        with pytest.raises(ApiError) as exc_info:
            anon.version()
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "unauthorized"


def test_version(client):
    info = client.version()
    assert info.version == __version__
    assert info.api == "v1"


def test_agents_empty_when_idle(client):
    assert client.agents_list() == []
