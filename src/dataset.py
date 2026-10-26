from io import TextIOWrapper
import json
import os
import threading
import time
from typing import Annotated, Any, Callable, Dict, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field, PlainSerializer, ConfigDict, AliasChoices
import logging

logger = logging.getLogger(__name__)


class DataFrame(BaseModel):
    timestamp: Annotated[
        datetime,
        PlainSerializer(lambda dt: dt.isoformat(timespec="milliseconds"), return_type=str)
    ] = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
        validation_alias=AliasChoices("TIMESTAMP", "timestamp"),
        serialization_alias="TIMESTAMP",
    )
    id: str = Field(
        validation_alias=AliasChoices("ID", "id"),
        serialization_alias="ID",
    )
    payload: Any = Field(
        validation_alias=AliasChoices("PAYLOAD", "payload"),
        serialization_alias="PAYLOAD",
    )

    model_config = ConfigDict(serialize_by_alias=True, populate_by_name=True)


class DatasetReader:

    def __init__(self, header: str = "X-v0.2"):
        if not isinstance(header, str):
            raise TypeError("header must be a str")
        self._header = header

        self._file: TextIOWrapper = None
        self._next_line: str = None
        # Byte-offset (an opaque text-stream tell() cookie, captured at a line
        # boundary) of the frame that read()/peek() will return next. Lets callers
        # index frame positions and later reopen positioned exactly at a frame line
        # (no partial-line reassembly, since offsets are always line-start).
        self._next_offset: Optional[int] = None

    @property
    def is_open(self) -> bool:
        return self._file is not None

    @property
    def next_offset(self) -> Optional[int]:
        """tell() cookie of the next frame line (the one read()/peek() returns),
        or None if not open. Pass it to open_at() to resume from this frame."""
        return self._next_offset

    @property
    def is_finished(self) -> bool:
        if not self.is_open:
            return True
        
        return self._next_line is None or self._next_line == ""

    def open(self, path: str) -> "DatasetReader":
        if not isinstance(path, str):
            raise TypeError("path must be a str")
        self._path = path

        if self._file is not None:
            raise Exception("file is already open")
        
        if not os.path.exists(self._path):
            raise FileExistsError(f"file '{self._path}' does not exist.")
        if not os.path.isfile(self._path):
            raise FileExistsError(f"file '{self._path}' is not a file.")
        self._file = open(self._path, 'r')

        header = self._file.readline().rstrip("\n")
        if header != self._header:
            self._file.close()
            raise Exception(f"dataset header '{header}' does not match expected header: '{self._header}'")

        self._next_offset = self._file.tell()
        self._next_line = self._file.readline()
        return self

    def open_at(self, path: str, offset: int) -> "DatasetReader":
        """Open ``path`` and position the reader at the frame line starting at
        ``offset`` (a cookie previously returned by ``next_offset``). The header
        is still validated. Used to resume playback from an indexed frame without
        re-reading everything before it."""
        if not isinstance(path, str):
            raise TypeError("path must be a str")
        if self._file is not None:
            raise Exception("file is already open")
        if not os.path.isfile(path):
            raise FileExistsError(f"file '{path}' is not a file.")
        self._path = path
        self._file = open(self._path, 'r')
        header = self._file.readline().rstrip("\n")
        if header != self._header:
            self._file.close()
            raise Exception(f"dataset header '{header}' does not match expected header: '{self._header}'")
        self._file.seek(offset)
        self._next_offset = offset
        self._next_line = self._file.readline()
        return self

    def close(self) -> None:
        if self._file is None:
            raise Exception("file is not open")

        self._file.close()

    def read(self) -> DataFrame:
        if self._file is None:
            raise Exception("file is not open")

        frame = DataFrame.model_validate_json(self._next_line)
        self._next_offset = self._file.tell()
        self._next_line = self._file.readline()
        if self._next_line is None or self._next_line == "":
            self.close()

        return frame
    
    def peek(self) -> DataFrame:
        if self._file is None:
            raise Exception("file is not open")
        
        return DataFrame.model_validate_json(self._next_line)

    def __enter__(self) -> "DatasetReader":
        return self.open()

    def __exit__(self, *_) -> None:
        self.close()


class DatasetWriter:

    def __init__(self, header: str = "X-v0.2"):
        if not isinstance(header, str):
            raise TypeError("header must be a str")
        self._header = header

        self._file: TextIOWrapper = None

    @property
    def is_open(self) -> bool:
        return self._file is not None

    def open(self, path: str) -> "DatasetWriter":
        if not isinstance(path, str):
            raise TypeError("path must be a str")
        self._path = path

        if self._file is not None:
            raise Exception("file is already open")
        
        self._file = open(self._path, 'w')
        self._file.write(f"{self._header}\n")
        return self

    def close(self) -> None:
        if self._file is None:
            raise Exception("file is not open")
        
        self._file.close()

    def write(self, frame: DataFrame):
        if self._file is None:
            raise Exception("file is not open")
        
        if not isinstance(frame, DataFrame):
            raise TypeError("frame must be a DataFrame")
        if frame.timestamp.tzinfo is None:
            frame.timestamp = frame.timestamp.astimezone(tz=timezone.utc)

        self._file.write(frame.model_dump_json() + "\n")

    def __enter__(self) -> "DatasetWriter":
        return self.open()

    def __exit__(self, *_) -> None:
        self.close()


class DatasetPlayer:

    def __init__(self, on_frame: Callable[["DatasetPlayer", DataFrame], None], header: str = "X-v0.2"):
        if not isinstance(on_frame, Callable):
            raise TypeError("on_frame must be a Callable")
        self._on_frame = on_frame

        if not isinstance(header, str):
            raise TypeError("header must be a str")
        self._header = header

        self._reader: Optional[DatasetReader] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_alive = False
        self._is_complete = False

    def play(self, file_path: str) -> "DatasetPlayer":
        if not isinstance(file_path, str):
            raise TypeError("file_path must be a str")
        
        if self._thread is not None:
            raise RuntimeError("Already playing")
        
        if not self._reader.is_open:
            self._reader.open()
        self._is_alive = False
        self._is_complete = False
        self._reader = DatasetReader(self._header).open(file_path)        
        self._thread = threading.Thread(
            target=self._loop,
            name="dataset-player",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def __enter__(self) -> "DatasetPlayer":
        return self.play()

    def __exit__(self, *_) -> None:
        self.stop()

    def _loop(self) -> None:
        try:
            self._is_alive = True
            self._is_complete = False
            start = datetime.now(timezone.utc)
            frame_start: Optional[datetime] = None
            logger.debug("Thread started")

            while not self._reader.is_finished:
                if self._stop_event.is_set():
                    logger.debug("Thread stop received")
                    break

                # Get the next frame and determine when it should be played
                delta = datetime.now(timezone.utc) - start
                frame = self._reader.read()
                if frame_start is None:
                    frame_start = frame.timestamp
                frame_delta = frame.timestamp - frame_start
                
                if frame_delta.total_seconds() > 120: # 2 minutes
                    raise Exception(f"Invalid frame_delta: {frame_delta}")

                # Wait for frame time
                while frame_delta > delta:
                    if self._stop_event.is_set():
                        logger.debug("Thread stop received")
                        return
                    time.sleep(0.001)
                    delta = datetime.now(timezone.utc) - start

                # Call the on_frame callback function
                self._on_frame(self, frame)

            self._is_complete = True
            logger.debug("ShdrDataPlayer finished")
            while True:
                if self._stop_event.is_set():
                    logger.debug("Thread stop received")
                    break
                time.sleep(0.1)

        except Exception as ex:
            logger.error(f"Thread exception: {ex}")
        finally:
            if self._reader is not None:
                self._reader.close()
                self._reader = None
            self._is_complete = True
            self._is_alive = False
