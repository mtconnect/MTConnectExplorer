import os
import socket
import threading
import logging
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Type, Union
from abc import ABC, abstractmethod

import xml.etree.ElementTree as ET

from pydantic import BaseModel, Field, field_validator, model_validator

from .dataset import DatasetReader

logger = logging.getLogger(__name__)


class ShdrConditionLevel(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    FAULT = "FAULT"
    UNAVAILABLE = "UNAVAILABLE"


class ShdrObservation(ABC, BaseModel):
    """Base class for all SHDR observations."""

    timestamp: Optional[datetime] = None

    model_config = {"arbitrary_types_allowed": True}

    def _ts(self) -> str:
        ts = self.timestamp or datetime.now(timezone.utc)
        return ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    @abstractmethod
    def to_shdr(self) -> str:
        raise NotImplementedError()


class ShdrDataItem(ShdrObservation):
    """A single sample or event. Value of None is sent as UNAVAILABLE."""

    name: str
    value: Any = None

    def to_shdr(self) -> str:
        v = "UNAVAILABLE" if self.value is None else str(self.value)
        return f"{self._ts()}|{self.name}|{v}"


class ShdrCondition(ShdrObservation):
    """
    A condition observation.

    Wire format: timestamp|name|level|native_code|native_severity|qualifier|message
    """

    name: str
    level: ShdrConditionLevel
    native_code: str = ""
    native_severity: str = ""
    qualifier: str = ""
    message: str = ""

    def to_shdr(self) -> str:
        return "|".join([
            self._ts(),
            self.name,
            self.level.value,
            self.native_code,
            self.native_severity,
            self.qualifier,
            self.message,
        ])


class ShdrDataSet(ShdrObservation):
    """
    A DATA_SET observation (space-delimited key=value pairs).

    Set a value to None or "" to delete that entry.
    Set reset_triggered to a trigger name (e.g. "MANUAL") to reset the set.
    """

    name: str
    entries: Dict[str, Any]
    reset_triggered: Optional[str] = None

    def to_shdr(self) -> str:
        parts: List[str] = []
        if self.reset_triggered:
            parts.append(f":{self.reset_triggered}")
        for k, v in self.entries.items():
            parts.append(k if (v is None or v == "") else f"{k}={v}")
        return f"{self._ts()}|{self.name}|{' '.join(parts)}"


class ShdrTable(ShdrObservation):
    """
    A TABLE observation (nested key=value rows).

    Set reset_triggered to a trigger name to reset the table.
    An empty row (no cells) is emitted as a key-only token, which deletes that row.
    """

    name: str
    rows: Dict[str, Dict[str, Any]]
    reset_triggered: Optional[str] = None

    def to_shdr(self) -> str:
        parts: List[str] = []
        if self.reset_triggered:
            parts.append(f":{self.reset_triggered}")
        for row_key, row_vals in self.rows.items():
            if not row_vals:
                parts.append(row_key)  # empty row → delete
            else:
                inner = " ".join(f"{k}={v}" for k, v in row_vals.items())
                parts.append(f"{row_key}={{{inner}}}")
        return f"{self._ts()}|{self.name}|{' '.join(parts)}"


class ShdrTimeSeries(ShdrObservation):
    """A time series observation with an optional sampling frequency in Hz."""

    name: str
    values: List[float]
    frequency: Optional[float] = None

    def to_shdr(self) -> str:
        freq = str(self.frequency) if self.frequency is not None else ""
        vals = " ".join(str(v) for v in self.values)
        return f"{self._ts()}|{self.name}|{len(self.values)}|{freq}|{vals}"


class ShdrAsset(ShdrObservation):
    """Send or update an asset using the multiline format."""

    asset_id: str
    asset_type: str
    content: str  # XML body

    def to_shdr(self) -> str:
        boundary = uuid.uuid4().hex.upper()
        return (
            f"{self._ts()}|@ASSET@|{self.asset_id}|{self.asset_type}"
            f"|--multiline--{boundary}\n{self.content}\n--multiline--{boundary}"
        )


class ShdrRemoveAsset(ShdrObservation):
    """Remove a single asset by ID."""

    asset_id: str

    def to_shdr(self) -> str:
        return f"{self._ts()}|@REMOVE_ASSET@|{self.asset_id}"


class ShdrRemoveAllAssets(ShdrObservation):
    """Remove all assets of a given type."""

    asset_type: str

    def to_shdr(self) -> str:
        return f"{self._ts()}|@REMOVE_ALL_ASSETS@|{self.asset_type}"


def get_observation_type(name: str) -> Type[ShdrObservation]:
    if not isinstance(name, str):
        raise TypeError("name must be a str")

    for t in [
        ShdrDataItem,
        ShdrCondition,
        ShdrDataSet,
        ShdrTable,
        ShdrTimeSeries,
        ShdrAsset,
        ShdrRemoveAsset,
        ShdrRemoveAllAssets,
    ]:
        if name == t.__name__:
            return t
        
    raise Exception("Invalid Observation Type Name")
    

class ShdrAdapterConfiguration(BaseModel):
    device: str = Field(
        description="The device name or UUID",
    )

    device_model: str = Field(
        description="The device model XML content."
    )
    @field_validator("device_model")
    @classmethod
    def validate_device_model(cls, v: str) -> str:
        try:
            root = ET.fromstring(v)
        except ET.ParseError as e:
            raise ValueError(f"device_model is not valid XML: {e}") from e
        if root.tag != "Device" and not root.tag.endswith("}Device"):
            raise ValueError(
                f"device_model root tag must be <Device>, got <{root.tag}>"
            )
        return v

    host: str = Field(
        "0.0.0.0",
        description="The interface to bind to (default is all interfaces)"
    )

    port: int = Field(
        7878,
        description="The TCP port the agent will connect to."
    )

    heartbeat_ms: int = Field(
        10_000,
        description="Milliseconds advertised to the agent as the expected PING interval. " \
                    "Connections that miss two intervals are closed."
    )

    @model_validator(mode="after")
    def validate_device_matches_model(self) -> "ShdrAdapterConfiguration":
        root = ET.fromstring(self.device_model)
        name = root.get("name", "")
        uuid_ = root.get("uuid", "")
        if self.device not in (name, uuid_):
            raise ValueError(
                f"device '{self.device}' does not match <Device> name='{name}' or uuid='{uuid_}'"
            )
        return self


class ShdrAdapter:
    """
    MTConnect SHDR Adapter (TCP server).

    The MTConnect agent connects to this server. One instance represents one
    device. Call start() before sending data, stop() when done, or use as a
    context manager.
    """

    def __init__(
        self,
        configuration: ShdrAdapterConfiguration,
    ) -> None:
        if not isinstance(configuration, ShdrAdapterConfiguration):
            raise TypeError("configuration must be a ShdrAdapterConfiguration")
        self._configuration = configuration

        self._server_sock: Optional[socket.socket] = None
        self._agent_sockets: Dict[socket.socket, threading.Thread] = {}
        self._lock = threading.Lock()
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self._queue: List[str] = []

    @property
    def configuration(self) -> ShdrAdapterConfiguration:
        return self._configuration
    
    @property
    def agent_is_connected(self) -> bool:
        return len(self._agent_sockets) > 0

    def start(self) -> "ShdrAdapter":
        """Bind the port, start listening, and begin accepting agent connections."""
        self._running = True
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR only on POSIX. There it merely bypasses TIME_WAIT on rebind
        # and does NOT permit two live listeners on one port (that needs
        # SO_REUSEPORT), so it's safe and desirable. On Windows, SO_REUSEADDR lets a
        # SECOND socket bind a port a live listener already holds — a silent
        # double-bind where the agent may connect to a stale/orphaned adapter
        # instead of ours. Windows doesn't need it for restart (verified), so omit
        # it there and let a duplicate bind fail fast with WSAEADDRINUSE below.
        if os.name != "nt":
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server_sock.bind((self.configuration.host, self.configuration.port))
        except OSError as exc:
            raise OSError(
                f"SHDR adapter for device '{self.configuration.device}' could not bind "
                f"{self.configuration.host}:{self.configuration.port} — the port is already "
                f"in use (a previous session's adapter may still be running): {exc}"
            ) from exc
        self._server_sock.listen(5)
        self._server_thread = threading.Thread(
            target=self._accept_loop,
            name=f"shdr-accept-{self.configuration.device}",
            daemon=True,
        )
        self._server_thread.start()
        logger.debug(
            f"SHDR adapter '{self.configuration.device}' listening on {self.configuration.host}:{self.configuration.port:d}"
        )

        return self

    def stop(self) -> None:
        """Close all connections and shut down the server thread."""
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        with self._lock:
            for sock in list(self._agent_sockets):
                try:
                    sock.close()
                except OSError:
                    pass
            self._agent_sockets.clear()
        if self._server_thread:
            self._server_thread.join(timeout=5)
        logger.debug(f"SHDR adapter '{self.configuration.device}' stopped")

    def __enter__(self) -> "ShdrAdapter":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    def enqueue(self, obs: ShdrObservation | List[ShdrObservation]) -> None:
        """Enqueue observation(s) and flush to all connected agents."""
        if isinstance(obs, ShdrObservation):
            obs = [obs]
        if not isinstance(obs, list) or not all(isinstance(o, ShdrObservation) for o in obs):
            raise TypeError("obs must be a ShdrObservation | List[ShdrObservation]")

        now = datetime.now(timezone.utc)
        lines = []
        for o in obs:
            if o.timestamp is None:
                o = o.model_copy(update={"timestamp": now})
            lines.append(o.to_shdr())

        with self._lock:
            self._queue.extend(lines)

        #logger.debug(f"Enqueued {len(obs)} ShdrObservations")

        self._flush_queue()

    def send_command(self, key: str, value: str) -> None:
        """
        Broadcast a * command to all connected agents.

        Example: send_command("calibration", "XXX")
        sends:   * calibration: XXX
        """
        if not isinstance(key, str):
            raise TypeError("key must be a str")
        if not isinstance(value, str):
            raise TypeError("value must be a str")

        self._broadcast(f"* {key}: {value}")

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except OSError:
                break
            logger.debug(
                "Agent connected to adapter '%s' from %s",
                self.configuration.device, addr,
            )
            self._handshake(conn)
            t = threading.Thread(
                target=self._read_loop,
                args=(conn,),
                name=f"shdr-read-{addr}",
                daemon=True,
            )
            with self._lock:
                self._agent_sockets[conn] = t
            self._flush_queue()
            t.start()

    def _handshake(self, conn: socket.socket) -> None:
        """Send device identity (and optional device model) to the agent on connect."""
        try:
            # Device model must be sent first, before any other commands.
            if self.configuration.device_model is not None:
                boundary = uuid.uuid4().hex.upper()
                self._send_line(
                    conn,
                    f"* deviceModel: --multiline--{boundary}\n"
                    f"{self.configuration.device_model}\n"
                    f"--multiline--{boundary}",
                )
            self._send_line(conn, f"* device: {self.configuration.device}")
            self._send_line(conn, "* shdrVersion: 2.0")
        except OSError as exc:
            logger.warning("Handshake failed for '%s': %s", self.configuration.device, exc)

    def _read_loop(self, conn: socket.socket) -> None:
        """Per-connection thread: read from the agent and respond to PING."""
        timeout_s = (self.configuration.heartbeat_ms / 1000) * 2
        conn.settimeout(timeout_s)
        buf = ""
        try:
            while self._running:
                try:
                    chunk = conn.recv(1024).decode("utf-8", errors="replace")
                except socket.timeout:
                    logger.warning(
                        "Heartbeat timeout on agent connection for device '%s'",
                        self.configuration.device,
                    )
                    break
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if line.strip() == "* PING":
                        try:
                            self._send_line(conn, f"* PONG {self.configuration.heartbeat_ms}")
                        except OSError:
                            return
        except OSError:
            pass
        finally:
            with self._lock:
                self._agent_sockets.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass
            logger.debug("Agent connection closed for device '%s'", self.configuration.device)

    def _flush_queue(self) -> None:
        """Drain the pending queue to all currently connected agents."""
        with self._lock:
            if not self._queue or not self._agent_sockets:
                return
            lines = list(self._queue)
            self._queue.clear()
            sockets = list(self._agent_sockets)
        data = b"".join((line + "\n").encode("utf-8") for line in lines)
        dead: List[socket.socket] = []
        sent = False
        for sock in sockets:
            try:
                sock.sendall(data)
                sent = True
            except OSError:
                dead.append(sock)
        if dead:
            with self._lock:
                if not sent:
                    self._queue = lines + self._queue
                for sock in dead:
                    self._agent_sockets.pop(sock, None)
                    try:
                        sock.close()
                    except OSError:
                        pass
        #logger.debug(f"Sent {len(lines)} lines")

    def _broadcast(self, line: str) -> None:
        """Send an LF-terminated SHDR line to all connected agents."""
        data = (line + "\n").encode("utf-8")
        with self._lock:
            sockets = list(self._agent_sockets)
        dead: List[socket.socket] = []
        for sock in sockets:
            try:
                sock.sendall(data)
            except OSError:
                dead.append(sock)
        if dead:
            with self._lock:
                for sock in dead:
                    self._agent_sockets.pop(sock, None)
                    try:
                        sock.close()
                    except OSError:
                        pass

    def _send_line(self, conn: socket.socket, line: str) -> None:
        conn.sendall((line + "\n").encode("utf-8"))


class ShdrDataPlayer:

    def __init__(self, host: str = "localhost", port: int = 7878):
        if not isinstance(host, str):
            raise TypeError("host must be a str")
        self._host = host

        if not isinstance(port, int):
            raise TypeError("port must be an int")
        self._port = port        

        self._adapter: Optional[ShdrAdapter] = None

        self._reader = DatasetReader("Shdr-v0.1")

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_alive = False
        self._is_complete = False

    @property
    def adapter(self) -> ShdrAdapter:
        return self._adapter

    @property
    def is_alive(self) -> bool:
        return self._is_alive

    def start(self, file_path: str, *, autostart: bool = True, wait_for_connect_timeout_s: Optional[float] = None) -> "ShdrDataPlayer":
        if not isinstance(file_path, str):
            raise TypeError("file_path must be a str")
        
        if not isinstance(autostart, bool):
            raise TypeError("autostart must be a bool")
        
        if wait_for_connect_timeout_s is not None:
            if not isinstance(wait_for_connect_timeout_s, float):
                raise TypeError("wait_for_connect_timeout must be an float")
            if wait_for_connect_timeout_s < 0:
                raise ValueError("wait_for_connect_timeout must be positive")

        if self._adapter is not None:
            raise RuntimeError("ShdrDataPlayer is already started")
        if self._thread is not None:
            raise RuntimeError("ShdrDataPlayer is already playing")
        self._reader.open(file_path)

        frame = self._reader.read()
        configuration = ShdrAdapterConfiguration(
            device=frame.id,
            device_model=frame.payload,
            host=self._host,
            port=self._port
        )
        self._adapter = ShdrAdapter(configuration)
        self._adapter.start()
        
        if wait_for_connect_timeout_s is not None:
            timeout_time = time.time() + (wait_for_connect_timeout_s)
            while not self._adapter.agent_is_connected:
                if time.time() > timeout_time:
                    raise TimeoutError("timeout exceeded")
                time.sleep(0.001)
            time.sleep(1.0)

        if autostart:
            self.play()
            
        return self
    
    def play(self) -> None:
        if self._thread is not None:
            raise RuntimeError("ShdrDataPlayer is already playing")
        self._thread = threading.Thread(
            target=self._loop,
            name="mtconnect-shdr-player",
            daemon=True,
        )
        self._thread.start()
    
    def stop(self) -> None:
        """
        Signal to stop and wait for the background thread to exit.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def __enter__(self) -> "ShdrDataPlayer":
        return self.start()

    def __exit__(self, *_) -> None:
        self.stop()

    def _loop(self) -> None:
        try:
            self._is_alive = True
            self._is_complete = False
            start = datetime.now(timezone.utc)
            frame_start: Optional[datetime] = None
            logger.debug("ShdrDataPlayer thread started")

            while not self._reader.is_finished:
                if self._stop_event.is_set():
                    logger.debug("ShdrDataPlayer thread stop received")
                    break

                delta = datetime.now(timezone.utc) - start
                frame = self._reader.read()
                if frame_start is None:
                    frame_start = frame.timestamp
                frame_delta = frame.timestamp - frame_start
                if frame_delta.total_seconds() > 120: # 2 minutes
                    raise Exception(f"Invalid frame_delta: {frame_delta}")

                while frame_delta > delta:
                    if self._stop_event.is_set():
                        logger.debug("ShdrDataPlayer thread stop received")
                        return
                    time.sleep(0.001)
                    delta = datetime.now(timezone.utc) - start

                obs_type = get_observation_type(frame.id)
                observation = obs_type.model_validate(frame.payload)
                self._adapter.enqueue(observation)

            self._is_complete = True
            logger.debug("ShdrDataPlayer finished")
            while True:
                if self._stop_event.is_set():
                    logger.debug("ShdrDataPlayer thread stop received")
                    break
                time.sleep(0.1)

        except Exception as ex:
            logger.error(f"ShdrDataPlayer thread exception: {ex}")
        finally:
            self._adapter.stop()
            self._reader.close()
            self._is_alive = False
            logger.debug("ShdrDataPlayer thread exited")
