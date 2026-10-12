"""Dataset service (docs/api.md §6): the one authoritative owner of the
datasets directory — listing, in-use status, rename, delete, name generation,
sanitization, and the path-traversal guard.
"""

import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Dict, List

from mtconnect_explorer_client.schemas import DatasetDevices, DatasetInfo, DeviceInfo

from .errors import Conflict, Invalid, NotFound, TooLarge
from . import sessions

EXT = ".jsonl"
INVALID_NAME_CHARS = set('<>:"/\\|?*')

# Upload cap (docs/api.md §6): 5 GiB, matching the UI's maxUploadSize.
MAX_UPLOAD_BYTES = 5120 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024  # 1 MB, per the reference services

# Serializes the exists-check + atomic move so two concurrent uploads of the
# same name can't both land (os.rename overwrites on POSIX).
_upload_lock = threading.Lock()

_base_dir = Path("datasets")


def base_dir() -> Path:
    return _base_dir


def set_base_dir(path: Path) -> None:
    """Override the datasets directory (tests)."""
    global _base_dir
    _base_dir = Path(path)


def ensure_dir() -> Path:
    _base_dir.mkdir(parents=True, exist_ok=True)
    return _base_dir


def validate_name(name: str) -> None:
    """Sanitize a dataset filename (docs/api.md §6): must end in .jsonl, no path
    separators or  < > : " / \\ | ? * , no leading dots, and the resolved path
    must stay inside the datasets directory. Raises Invalid(invalid_name)."""
    def bad(reason: str) -> Invalid:
        return Invalid(f"invalid dataset name '{name}': {reason}", code="invalid_name")

    if not name or not name.lower().endswith(EXT):
        raise bad(f"must end in {EXT}")
    if name.startswith("."):
        raise bad("must not start with a dot")
    if any(ch in INVALID_NAME_CHARS for ch in name):
        raise bad('must not contain any of < > : " / \\ | ? *')
    base = ensure_dir().resolve()
    resolved = (base / name).resolve()
    if resolved.parent != base:
        raise bad("must resolve inside the datasets directory")


def dataset_path(name: str) -> Path:
    """Validated (but not existence-checked) path of a dataset."""
    validate_name(name)
    return _base_dir / name


def existing_path(name: str) -> Path:
    """Validated path of an existing dataset. Raises NotFound(dataset_not_found)."""
    path = dataset_path(name)
    if not path.is_file():
        raise NotFound(f"dataset '{name}' not found", code="dataset_not_found")
    return path


# -- in-use status -----------------------------------------------------------
def in_use_map() -> Dict[str, List[str]]:
    """Map of dataset name -> scopes using it ("play"/"capture"), derived from
    the live session tables (the one authoritative answer)."""
    uses: Dict[str, set] = {}
    for sess in sessions.all_live_sessions():
        if sess.scope == "play":
            uses.setdefault(sess.config.dataset, set()).add("play")
        elif sess.scope == "capture":
            filename = getattr(sess.config, "filename", None)
            if filename:
                uses.setdefault(filename, set()).add("capture")
    return {name: sorted(scopes) for name, scopes in uses.items()}


def in_use(name: str) -> List[str]:
    return in_use_map().get(name, [])


# -- queries ------------------------------------------------------------------
def _info(path: Path, uses: Dict[str, List[str]]) -> DatasetInfo:
    stat = path.stat()
    return DatasetInfo(
        name=path.name,
        size_bytes=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        in_use=uses.get(path.name, []),
    )


def list_datasets() -> List[DatasetInfo]:
    """All datasets, newest first (by mtime)."""
    ensure_dir()
    uses = in_use_map()
    files = sorted(_base_dir.glob(f"*{EXT}"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [_info(f, uses) for f in files]


def get_dataset(name: str) -> DatasetInfo:
    return _info(existing_path(name), in_use_map())


def get_device_names(name: str) -> List[str]:
    """Device names from the dataset's probe frame. Raises Invalid(invalid_dataset)
    when the header or first frame isn't a valid probe."""
    return [d.name for d in get_devices(name).devices]


def get_devices(name: str) -> DatasetDevices:
    """All devices (id/name/uuid) from the dataset's probe frame, including the
    Agent device — filtering it out is a frontend concern (docs/api.md §6)."""
    from src.client import MtConnectClientFrame
    from src.dataset import DatasetReader

    path = existing_path(name)
    try:
        reader = DatasetReader("RawMTConnect-v0.1")
        reader.open(str(path))
        try:
            frame = MtConnectClientFrame.model_validate(reader.read().payload)
        finally:
            # Close promptly — a dangling handle blocks rename/delete on Windows.
            reader.close()
        if frame.type != "probe":
            raise ValueError("first frame must be a probe request/response")
        devices = frame.parse_probe_devices()
    except Exception as ex:
        raise Invalid(f"dataset '{name}' is not a valid dataset: {ex}",
                      code="invalid_dataset")
    return DatasetDevices(devices=sorted(
        (DeviceInfo(id=d.id, name=d.name, uuid=d.uuid) for d in devices),
        key=lambda d: d.name,
    ))


# -- mutation ------------------------------------------------------------------
def rename(name: str, new_name: str) -> None:
    path = existing_path(name)
    validate_name(new_name)
    if new_name == name:
        return
    used = in_use(name)
    if used:
        raise Conflict(f"dataset '{name}' is in use ({', '.join(used)})",
                       code="dataset_in_use")
    target = _base_dir / new_name
    if target.exists():
        raise Conflict(f"dataset '{new_name}' already exists", code="dataset_exists")
    path.rename(target)


def delete(name: str) -> None:
    path = existing_path(name)
    used = in_use(name)
    if used:
        raise Conflict(f"dataset '{name}' is in use ({', '.join(used)})",
                       code="dataset_in_use")
    path.unlink()


def save_upload(
    name: str,
    fileobj: BinaryIO,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> DatasetInfo:
    """Stream an upload to a temp file in 1 MB chunks, then atomically move it
    into the datasets directory — a partially-uploaded file is never visible to
    Play/Capture. Overwriting is not allowed (docs/api.md §6)."""
    validate_name(name)
    base = ensure_dir()

    def check_target() -> None:
        if in_use(name):
            raise Conflict(f"dataset '{name}' is in use", code="dataset_in_use")
        if (base / name).exists():
            raise Conflict(f"dataset '{name}' already exists", code="dataset_exists")

    check_target()  # Fail fast before receiving gigabytes.
    tmp = tempfile.NamedTemporaryFile(
        dir=base, prefix=".upload-", suffix=".part", delete=False
    )
    tmp_path = Path(tmp.name)
    try:
        total = 0
        while True:
            chunk = fileobj.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise TooLarge(
                    f"upload exceeds the {max_bytes} byte cap", code="too_large"
                )
            tmp.write(chunk)
        tmp.close()
        with _upload_lock:
            check_target()
            tmp_path.rename(base / name)
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        tmp_path.unlink(missing_ok=True)
        raise
    return get_dataset(name)


# -- capture filename generation ----------------------------------------------
def _capture_filename(base_url: str) -> str:
    cleaned = re.sub(
        r"_+", "_",
        re.sub(r"[^a-zA-Z0-9._-]", "_", base_url.replace("://", "_").rstrip("/"))
    ).strip("_")
    return f"{cleaned}-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}{EXT}"


def generate_capture_filename(base_url: str, active: set) -> str:
    """A dataset filename for a new capture that doesn't collide with an existing
    file or another in-flight capture (``active`` = filenames being written)."""
    ensure_dir()
    name = _capture_filename(base_url)
    if not (_base_dir / name).exists() and name not in active:
        return name
    stem, _, ext = name.rpartition(".")
    i = 2
    while True:
        candidate = f"{stem}-{i}.{ext}"
        if not (_base_dir / candidate).exists() and candidate not in active:
            return candidate
        i += 1
