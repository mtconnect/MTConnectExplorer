import io

import pytest

from mtconnect_explorer_client import ApiError

pytestmark = pytest.mark.usefixtures("clean_datasets")

CONTENT = b'RawMTConnect-v0.1\n{"TIMESTAMP": 1, "ID": "x", "PAYLOAD": {}}\n'


def _upload(client, name, content=CONTENT):
    return client.datasets_upload(name, io.BytesIO(content))


def test_upload_list_get_download(client):
    info = _upload(client, "a.jsonl")
    assert info.name == "a.jsonl"
    assert info.size_bytes == len(CONTENT)
    assert info.in_use == []

    assert [d.name for d in client.datasets_list()] == ["a.jsonl"]
    assert client.datasets_get("a.jsonl").size_bytes == len(CONTENT)
    assert b"".join(client.datasets_download("a.jsonl")) == CONTENT


def test_upload_no_overwrite(client):
    _upload(client, "a.jsonl")
    with pytest.raises(ApiError) as exc_info:
        _upload(client, "a.jsonl")
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "dataset_exists"


def test_upload_wrong_extension_is_415(client):
    with pytest.raises(ApiError) as exc_info:
        _upload(client, "a.txt")
    assert exc_info.value.status_code == 415
    assert exc_info.value.code == "unsupported_type"


def test_upload_invalid_names_are_422(client):
    for bad in (".hidden.jsonl", "a?b.jsonl", "a<b>.jsonl"):
        with pytest.raises(ApiError) as exc_info:
            _upload(client, bad)
        assert exc_info.value.status_code == 422, bad
        assert exc_info.value.code == "invalid_name", bad


def test_rename(client):
    _upload(client, "a.jsonl")
    client.datasets_rename("a.jsonl", "b.jsonl")
    assert [d.name for d in client.datasets_list()] == ["b.jsonl"]


def test_rename_conflicts_and_traversal(client):
    _upload(client, "a.jsonl")
    _upload(client, "b.jsonl")
    with pytest.raises(ApiError) as exc_info:
        client.datasets_rename("a.jsonl", "b.jsonl")
    assert exc_info.value.code == "dataset_exists"

    with pytest.raises(ApiError) as exc_info:
        client.datasets_rename("a.jsonl", "../evil.jsonl")
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_name"

    with pytest.raises(ApiError) as exc_info:
        client.datasets_rename("a.jsonl", "evil.txt")
    assert exc_info.value.code == "invalid_name"


def test_delete(client):
    _upload(client, "a.jsonl")
    client.datasets_delete("a.jsonl")
    assert client.datasets_list() == []
    with pytest.raises(ApiError) as exc_info:
        client.datasets_delete("a.jsonl")
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "dataset_not_found"


def test_devices_of_invalid_dataset_is_422(client):
    _upload(client, "garbage.jsonl", b"not a dataset at all\n")
    with pytest.raises(ApiError) as exc_info:
        client.datasets_devices("garbage.jsonl")
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_dataset"


def test_unknown_dataset_is_404(client):
    with pytest.raises(ApiError) as exc_info:
        client.datasets_get("nope.jsonl")
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "dataset_not_found"
