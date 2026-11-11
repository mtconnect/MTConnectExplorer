"""Dataset endpoints (docs/api.md §6)."""

from typing import List

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import FileResponse

from mtconnect_explorer_client.schemas import (
    DatasetDevices,
    DatasetInfo,
    RenameRequest,
)

from src.services import datasets

from ..errors import http_error
from ..security import require_api_key

router = APIRouter(
    prefix="/datasets",
    tags=["Datasets"],
    dependencies=[Depends(require_api_key)],
)


@router.get(
    "",
    summary="List all datasets (newest first) with in-use status",
    response_model=List[DatasetInfo],
)
def list_datasets() -> List[DatasetInfo]:
    return datasets.list_datasets()


@router.post(
    "/upload",
    summary="Upload a dataset (multipart; overwriting is not allowed)",
    response_model=DatasetInfo,
    status_code=201,
)
def upload(file: UploadFile) -> DatasetInfo:
    name = file.filename or ""
    if not name.lower().endswith(datasets.EXT):
        raise http_error(
            415, "unsupported_type",
            f"upload must be a {datasets.EXT} dataset (got '{name}')",
        )
    # Starlette has already spooled the part to a temp file; stream it in 1 MB
    # chunks to a temp file beside the datasets dir and atomically move it in.
    return datasets.save_upload(name, file.file)


@router.get(
    "/{name}",
    summary="Dataset metadata",
    response_model=DatasetInfo,
)
def get_dataset(name: str) -> DatasetInfo:
    return datasets.get_dataset(name)


@router.get(
    "/{name}/content",
    summary="Download the dataset file (streamed)",
    response_class=FileResponse,
)
def download(name: str) -> FileResponse:
    path = datasets.existing_path(name)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=name,
    )


@router.get(
    "/{name}/devices",
    summary="Devices from the dataset's probe frame (including the Agent device)",
    response_model=DatasetDevices,
)
def devices(name: str) -> DatasetDevices:
    return datasets.get_devices(name)


@router.patch(
    "/{name}",
    summary="Rename a dataset",
    status_code=204,
)
def rename(name: str, body: RenameRequest) -> None:
    datasets.rename(name, body.new_name)


@router.delete(
    "/{name}",
    summary="Delete a dataset",
    status_code=204,
)
def delete(name: str) -> None:
    datasets.delete(name)
