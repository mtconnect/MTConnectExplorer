from datetime import datetime, timezone
import http.client
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any, Dict, List, Literal, Optional, Tuple, Annotated
import xml.etree.ElementTree as ET
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from .dataset import DataFrame, DatasetWriter

logger = logging.getLogger(__name__)


class MtConnectObservation(BaseModel):
    timestamp: Annotated[
        datetime, 
        PlainSerializer(lambda dt: dt.isoformat(timespec="milliseconds"), return_type=str)
    ] = Field(
        description="Timestamp attribute",
    )
    tag: str = Field(
        description="XML Tag Name"
    )
    attributes: Dict[str, str] = Field(
        description="XML Tag Attributes"
    )
    text: Optional[str] = Field(
        description="XML Tag Text"
    )


class MtConnectDevice(BaseModel):
    id: str = Field(
        description="Device id attribute",
    )
    name: str = Field(
        description="Device name attribute",
    )
    uuid: str = Field(
        description="Device uuid attribute",
    )
    tag: str = Field(
        description="XML Tag Name"
    )
    attributes: Dict[str, str] = Field(
        description="XML Tag Attributes"
    )
    model: str = Field(
        description="Device XML model"
    )


class MtConnectDeviceStream(BaseModel):
    id: str = Field(

    )
    name: str = Field(

    )
    uuid: str = Field(
        description="Device uuid attribute",
    )
    tag: str = Field(
        description="XML Tag Name"
    )
    attributes: Dict[str, str] = Field(
        description="XML Tag Attributes"
    )
    observations: List[MtConnectObservation] = Field(
        description="Device Observations"
    )


class MtConnectClientFrame(BaseModel):
    timestamp: Annotated[
        datetime, 
        PlainSerializer(lambda dt: dt.isoformat(timespec="milliseconds"), return_type=str)
    ] = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
        description="When the response was received.",
    )
    type: Literal["probe", "current", "sample"] = Field(
        description="The type of MTConnect request",
    )
    parameters: Dict[str, Any] = Field(
        description="The parameters used to make the MTConnect request",
    )
    url: str = Field(
        description="The URL of the MTConnect request",
    )
    response: str = Field(
        description="The MTConnect agent response",
    )
    exception: Optional[str] = Field(
        None,
        description="A description of an exception if thrown/raised.",
    )

    def parse_header(self) -> dict:
        root = ET.fromstring(self.response)
        header = next(el for el in root.iter() if el.tag.endswith("}Header") or el.tag == "Header")
        return header.attrib

    def parse_stream_next_sequence(self) -> int:
        if self.type == "probe":
            raise Exception("probe request does not contain sequence numbers")
        return int(self.parse_header()["nextSequence"])
    
    def parse_probe_devices(self) -> List[MtConnectDevice]:
        if self.type != "probe":
            raise Exception("probe request contains device information")
        
        root_el = ET.fromstring(self.response)
        self._remove_all_namespaces_recursive(root_el)
        devices_el = [el for el in root_el if el.tag.endswith("Devices")]
        if len(devices_el) != 1:
            raise Exception("Could not identify Devices tag")
        devices_el = devices_el[0]

        devices: List[MtConnectDevice] = []
        for device_el in devices_el:
            ET.indent(device_el, space="  ")
            devices.append(MtConnectDevice(
                id=device_el.attrib["id"],
                name=device_el.attrib["name"],
                uuid=device_el.attrib["uuid"],
                tag=device_el.tag.split("}")[-1],
                attributes=device_el.attrib,
                model=ET.tostring(device_el, encoding="unicode")
            ))

        return devices
    
    def parse_device_streams(self) -> List[MtConnectDeviceStream]:
        if self.type == "probe":
            raise Exception("probe request does not contain stream information")
        
        root_el = ET.fromstring(self.response)
        self._remove_all_namespaces_recursive(root_el)
        streams_el = [el for el in root_el if el.tag.endswith("Streams")]
        if len(streams_el) != 1:
            raise Exception("Could not identify Streams tag")
        streams_el = streams_el[0]

        devices: List[MtConnectDeviceStream] = []
        for stream_el in streams_el:
            observations: List[MtConnectObservation] = []
            for el in stream_el.iter():
                if not "dataItemId" in el.attrib:
                    continue
                observations.append(MtConnectObservation(
                    timestamp=el.attrib["timestamp"],
                    tag=el.tag.split("}")[-1],
                    attributes=el.attrib,
                    text=el.text,
                ))
            devices.append(MtConnectDeviceStream(
                id=stream_el.attrib.get("id", ""),
                name=stream_el.attrib["name"],
                uuid=stream_el.attrib["uuid"],
                tag=stream_el.tag.split("}")[-1],
                attributes=stream_el.attrib,
                observations=observations,
            ))

        # Sort all obserations by timestamp
        for device in devices:
            device.observations.sort(key=lambda x: x.timestamp)

        return devices
    
    def parse_observations(self) -> List[MtConnectObservation]:
        if self.type == "probe":
            raise Exception("probe request does not contain observations")
        observations: List[MtConnectObservation] = []
        for el in ET.fromstring(self.response).iter():
            if not "dataItemId" in el.attrib:
                continue
            observations.append(MtConnectObservation(
                timestamp=el.attrib["timestamp"],
                tag=el.tag.split("}")[-1],
                attributes=el.attrib,
                text=el.text,
            ))

        # TODO: need to sort by timestamp and/or sequence

        return observations
    
    def _remove_all_namespaces_recursive(self, root):
        """Recursively remove namespaces from tags and attributes."""
        for elem in root.iter():
            # Remove namespace from tag
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]
            
            # Remove namespace from attributes
            for attr in list(elem.attrib.keys()):
                if '}' in attr:
                    new_attr = attr.split('}', 1)[1]
                    elem.attrib[new_attr] = elem.attrib.pop(attr)


class MtConnectStreamReader:
    """
    Wraps a streaming iterator and drains it on a background thread.

    Received XML documents accumulate in ``items``.  Use ``is_alive`` to check
    whether the background thread is still running, and ``stop()`` to shut it
    down cleanly.
    """

    def __init__(self, template_frame: MtConnectClientFrame, stream: Iterator[str]) -> None:
        if not isinstance(template_frame, MtConnectClientFrame):
            raise TypeError("template_frame must be an MtConnectFrame")
        self._template_frame = template_frame

        if not isinstance(stream, Iterator):
            raise TypeError("stream must be an Iterator[str]")
        self._stream = stream

        self._items: list[MtConnectClientFrame] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._alive = False

    @property
    def template_frame(self) -> MtConnectClientFrame:
        return self._template_frame

    def start(self) -> "MtConnectStreamReader":
        """
        Start the background reader thread. Returns self for chaining.
        """
        if self._thread is not None:
            raise RuntimeError("StreamReader is already started")
        self._alive = True
        self._thread = threading.Thread(
            target=self._read_loop,
            name="mtconnect-stream-reader",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """
        Signal the reader to stop and wait for the background thread to exit.
        """
        self._stop_event.set()
        try:
            self._stream.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join()

    def __enter__(self) -> "MtConnectStreamReader":
        return self.start()

    def __exit__(self, *_) -> None:
        self.stop()

    @property
    def is_alive(self) -> bool:
        """
        True while the background thread is running and the stream is open.
        """
        return self._alive

    @property
    def count(self) -> int:
        """
        Number of XML documents currently held in the buffer.
        """
        with self._lock:
            return len(self._items)
        
    def pop(self) -> MtConnectClientFrame:
        with self._lock:
            item = self._items.pop(0)
        return item

    def drain(self) -> list[MtConnectClientFrame]:
        """
        Remove and return all buffered XML documents.
        """
        with self._lock:
            items = self._items
            self._items = []
            return items

    def peek(self) -> list[MtConnectClientFrame]:
        """
        Return a snapshot of buffered documents without removing them.
        """
        with self._lock:
            return list(self._items)

    def _read_loop(self) -> None:
        try:
            for xml in self._stream:
                if self._stop_event.is_set():
                    break

                # Copy the template and add the response
                frame = self._template_frame.model_copy(deep=True)
                frame.response = xml

                with self._lock:
                    self._items.append(frame)
            logger.debug("MtConnectStreamReader stream ended")
        except Exception as ex:
            logger.error(f"MtConnectStreamReader thread exception: {ex}")
        finally:
            self._alive = False
            logger.debug("MtConnectStreamReader thread exited")


class MtConnectClientStreamFrame(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    
    frame: MtConnectClientFrame = Field(
        description="The stream request frame"
    )
    reader: Optional[MtConnectStreamReader] = Field(
        description="A reader for the stream if no exceptions were thrown when initiating the stream."
    )


class MtConnectClientConfiguration(BaseModel):
    base_url: str = Field(
        "http://127.0.0.1:5000/",
        description="Base URL of the MTConnect agent, e.g. 'http://localhost:5000'",
        )
    timeout: float = Field(
        30.0,
        description="Socket timeout in seconds for non-streaming requests",
    )
    chunk_size: int = Field(
        4096,
        description="Read chunk size in bytes for streaming responses",
    )


class MtConnectClient:
    """
    HTTP client for an MTConnect agent.
    """

    def __init__(self, configuration: MtConnectClientConfiguration) -> None:
        if not isinstance(configuration, MtConnectClientConfiguration):
            raise TypeError("configuration must be an instance of MtConnectClientConfiguration")
        self._configuration = configuration

    @property
    def configuration(self) -> MtConnectClientConfiguration:
        return self._configuration

    def probe(
        self, 
        device: Optional[str] = None
    ) -> MtConnectClientFrame:
        """
        Return raw Devices XML from /probe (or /{device}/probe).
        """
        url = self._url(f"/{device}/probe" if device else f"/probe", {})

        exception: Optional[str] = None
        try:
            response = self._get(url)
        except Exception as ex:
            response = ""
            exception = str(ex)

        return MtConnectClientFrame(
            type="probe",
            parameters={"device": device},
            url=url,
            response=response,
            exception=exception,
        )

    def current(
        self,
        device: Optional[str] = None,
        *,
        path: Optional[str] = None,
        at: Optional[int] = None,
    ) -> MtConnectClientFrame:
        """
        Return current values from /current (or /{device}/current).

        Args:
            path: XPath filter for data items.
            at:   Return values at this sequence number.
        """
        params: dict = {"path": path, "at": at}
        url = self._url(f"/{device}/current" if device else f"/current", params)
        params.update({"device": device})

        exception: Optional[str] = None
        try:
            response = self._get(url)
        except Exception as ex:
            response = ""
            exception = str(ex)

        return MtConnectClientFrame(
            type="current",
            parameters=params,
            url=url,
            response=response,
            exception=exception,
        )

    def sample(
        self,
        device: Optional[str] = None,
        *,
        path: Optional[str] = None,
        from_sequence: Optional[int] = None,
        to: Optional[int] = None,
        count: Optional[int] = None,
    ) -> MtConnectClientFrame:
        """
        Return sampled values from /sample (or /{device}/sample).

        Args:
            path:          XPath filter for data items.
            from_sequence: Start at this sequence number (maps to 'from').
            to:            End at this sequence number.
            count:         Maximum number of observations to return.
        """
        params: dict = {
            "path": path,
            "from": from_sequence,
            "to": to,
            "count": count,
        }
        endpoint = f"/{device}/sample" if device else f"/sample"
        url = self._url(endpoint, params)
        params.update({"device": device})

        exception: Optional[str] = None
        try:
            response = self._get(url)
        except Exception as ex:
            response = ""
            exception = str(ex)

        return MtConnectClientFrame(
            type="sample",
            parameters=params,
            url=url,
            response=response,
            exception=exception,
        )

    def sample_stream(
        self,
        device: Optional[str] = None,
        *,
        path: Optional[str] = None,
        from_sequence: Optional[int] = None,
        count: Optional[int] = None,
        interval: int = 1000,
    ) -> MtConnectClientStreamFrame:
        """
        Open a streaming sample connection and return a started StreamReader.

        Call ``reader.stop()`` (or use as a context manager) to close the
        connection and join the background thread.

        Args:
            path:          XPath filter for data items.
            from_sequence: Start at this sequence number (maps to 'from').
                           Omit to stream only new data from the current time.
            count:         Maximum observations returned per interval tick.
            interval:      Milliseconds between agent buffer checks (default 1000).
        """
        params: dict = {
            "path": path,
            "from": from_sequence,
            "count": count,
            "interval": interval,
        }
        endpoint = f"/{device}/sample" if device else f"/sample"
        url = self._url(endpoint, params)
        params.update({"device": device})

        stream: Optional[Iterator[str]] = None
        exception: Optional[str] = None
        try:
            stream = self._stream(url)
        except Exception as ex:
            exception = str(ex)

        frame = MtConnectClientFrame(
            type="sample",
            parameters=params,
            url=url,
            response="",
            exception=exception,
        )

        reader: Optional[MtConnectStreamReader] = None
        if stream is not None:
            reader = MtConnectStreamReader(frame, stream).start()

        return MtConnectClientStreamFrame(
            frame=frame,
            reader=reader,
        )

    def _url(self, path: str, params: dict) -> str:
        base = self._configuration.base_url.rstrip("/")
        filtered = {k: v for k, v in params.items() if v is not None}
        query = urllib.parse.urlencode(filtered)
        return f"{base}{path}{'?' + query if query else ''}"

    def _get(self, url: str) -> str:
        logger.debug("GET %s", url)
        try:
            with urllib.request.urlopen(url, timeout=self._configuration.timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as ex:
            raise Exception(f"Error streaming, HTTPError, code: '{ex.code}', reason: '{ex.reason}', url: '{ex.url}'") from ex
        except urllib.error.URLError as ex:
            raise Exception(f"Error streaming, URLError, reason: '{ex.reason}', url: '{url}'") from ex

    def _stream(self, url: str) -> Iterator[str]:
        """
        Open a long-running connection and yield one XML document per multipart part.
        """
        logger.debug("STREAM %s", url)
        try:
            resp = urllib.request.urlopen(url)
        except urllib.error.HTTPError as ex:
            raise Exception(f"Error streaming, HTTPError, code: '{ex.code}', reason: '{ex.reason}', url: '{ex.url}'") from ex
        except urllib.error.URLError as ex:
            raise Exception(f"Error streaming, URLError, reason: '{ex.reason}', url: '{url}'") from ex

        try:
            content_type = resp.headers.get("Content-Type", "")
            boundary = self._parse_multipart_boundary(content_type)
            if boundary:
                try:
                    yield from self._iter_multipart_parts(resp, boundary, self._configuration.chunk_size)
                except GeneratorExit:
                    return
            else:
                # SSE or plain stream — yield raw text chunks
                while True:
                    chunk = resp.read(self._configuration.chunk_size)
                    if not chunk:
                        break
                    yield chunk.decode("utf-8", errors="replace")
        finally:
            resp.close()

    def _parse_multipart_boundary(self, content_type: str) -> Optional[str]:
        for segment in content_type.split(";"):
            segment = segment.strip()
            if segment.lower().startswith("boundary="):
                return segment[9:].strip('"')
        return None

    def _iter_multipart_parts(
        self,
        resp: http.client.HTTPResponse,
        boundary: str,
        chunk_size: int,
    ) -> Iterator[Optional[str]]:
        """
        Parse a multipart/x-mixed-replace stream and yield the XML body of each part.

        Buffers incoming bytes and emits one decoded string per complete part,
        stripping the MIME part headers that precede each XML body.
        """
        delimiter = f"--{boundary}".encode("ascii")
        end_delimiter = f"--{boundary}--".encode("ascii")
        buf = bytearray()

        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            buf.extend(chunk)

            # Consume as many complete parts as the buffer holds
            while True:
                start = buf.find(delimiter)
                if start == -1:
                    break

                # Detect end-of-stream marker
                tail = bytes(buf[start : start + len(end_delimiter)])
                if tail == end_delimiter:
                    return

                after_delim = start + len(delimiter)

                # Locate the next boundary so we know the full part
                next_start = buf.find(delimiter, after_delim)
                if next_start == -1:
                    # Part not yet complete — keep buffering, but discard bytes
                    # before the current delimiter so the buffer doesn't grow unbounded
                    del buf[:start]
                    break

                raw_part = bytes(buf[after_delim:next_start])
                del buf[:next_start]

                # Strip MIME part headers (blank line separates headers from body)
                header_end = raw_part.find(b"\r\n\r\n")
                if header_end != -1:
                    body = raw_part[header_end + 4 :]
                else:
                    header_end = raw_part.find(b"\n\n")
                    body = raw_part[header_end + 2 :] if header_end != -1 else raw_part

                body = body.strip()
                if body:
                    try:
                        yield body.decode("utf-8", errors="replace")
                    except GeneratorExit:
                        return


class MtConnectDataCollectorConfiguration(BaseModel):
    configuration: MtConnectClientConfiguration = Field(
        default_factory=MtConnectClientConfiguration,
        description="MTConnect client configuration",
    )
    stream_interval_ms: int = Field(
        1_000,
        description="Milliseconds between agent stream buffer checks",
    )
    mode: Literal["stream", "sample", "current"] = Field(
        default="stream",
        description="When stream, sample, or current requests should be used to collect data",
    )


class MtConnectDataCollector:

    def __init__(self, configuration: MtConnectDataCollectorConfiguration):
        if not isinstance(configuration, MtConnectDataCollectorConfiguration):
            raise TypeError("configuration must be a MtConnectDataCollectorConfiguration")
        self._configuration = configuration

        self._client = MtConnectClient(configuration.configuration)

        self._writer = DatasetWriter("RawMTConnect-v0.1")

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_alive = False
        self._is_collecting = False

    @property
    def client(self) -> MtConnectClient:
        return self._client
    
    @property
    def is_alive(self) -> bool:
        return self._is_alive
    
    @property
    def is_collecting(self) -> bool:
        return self._is_collecting
    
    def start(self, file_path: str) -> "MtConnectDataCollector":
        """
        Start the background thread. Returns self for chaining.
        """
        if not isinstance(file_path, str):
            raise TypeError("file_path must be a str")
        self._writer.open(file_path)
        if self._thread is not None:
            raise RuntimeError("MtConnectDataCollector is already started")
        self._thread = threading.Thread(
            target=self._loop,
            name="mtconnect-data-collector",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """
        Signal to stop and wait for the background thread to exit.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def __enter__(self) -> "MtConnectDataCollector":
        return self.start()

    def __exit__(self, *_) -> None:
        self.stop()

    def _loop(self) -> None:
        reader = None
        try:
            self._is_alive = True
            logger.debug("MtConnectDataCollector thread started")

            # Capture and write probe
            self._writer.write(DataFrame(
                id=MtConnectClientFrame.__name__,
                payload=self._client.probe(),
            ))
            logger.debug("Captured Probe")

            # Capture and write current
            current_response = self._client.current()
            next_sequence = current_response.parse_stream_next_sequence()
            self._writer.write(DataFrame(
                id=MtConnectClientFrame.__name__,
                payload=current_response,
            ))
            logger.debug("Captured Current")

            match self._configuration.mode:

                case "stream":
                    # Stream and write sample
                    stream_frame = self._client.sample_stream(from_sequence=next_sequence, interval=self._configuration.stream_interval_ms)
                    if stream_frame.reader is None:
                        raise Exception(f"Failed to start stream: {stream_frame.frame.exception or 'Unknown error'}")
                    reader = stream_frame.reader
                    logger.debug("Started Streaming Sample")
                    self._is_collecting = True
                    while reader.is_alive:
                        if self._stop_event.is_set():
                            logger.debug("Thread stop received")
                            break

                        if reader.count == 0:
                            time.sleep(0.1)
                            continue

                        logger.debug(f"Captured {reader.count} stream frames.")
                        while reader.count > 0:
                            self._writer.write(DataFrame(
                                id=MtConnectClientFrame.__name__,
                                payload=reader.pop(),
                            ))

                case "sample":
                    next_request_time = time.time() + (self._configuration.stream_interval_ms / 1_000.0)
                    while True:
                        # Wait for next request time
                        exit_flag = False
                        while True:
                            if self._stop_event.is_set():
                                logger.debug("Thread stop received")
                                exit_flag = True
                                break
                            if time.time() > next_request_time:
                                break
                            time.sleep(0.001)
                        if exit_flag:
                            break
                        # Update next request time
                        next_request_time = time.time() + (self._configuration.stream_interval_ms / 1_000.0)

                        # Make request
                        mtc_frame = self._client.sample(from_sequence=next_sequence)
                        next_sequence = mtc_frame.parse_stream_next_sequence()
                        self._writer.write(DataFrame(
                            id=MtConnectClientFrame.__name__,
                            payload=mtc_frame,
                        ))
                        logger.debug("Captured sample frame")

                case "current":
                    next_request_time = time.time() + (self._configuration.stream_interval_ms / 1_000.0)
                    while True:
                        # Wait for next request time
                        exit_flag = False
                        while True:
                            if self._stop_event.is_set():
                                logger.debug("Thread stop received")
                                exit_flag = True
                                break
                            if time.time() > next_request_time:
                                break
                            time.sleep(0.001)
                        if exit_flag:
                            break
                        # Update next request time
                        next_request_time = time.time() + (self._configuration.stream_interval_ms / 1_000.0)

                        # Make request
                        mtc_frame = self._client.current()
                        self._writer.write(DataFrame(
                            id=MtConnectClientFrame.__name__,
                            payload=mtc_frame,
                        ))
                        logger.debug("Captured current frame")
                        

        except Exception as ex:
            logger.error(f"Thread exception: {ex}")
        finally:
            if reader is not None:
                reader.stop()
            self._writer.close()
            self._is_alive = False
            self._is_collecting = False
            logger.debug("Thread exited")


class MtConnectExplorer:
    """
    Polls an MTConnect agent continuously and maintains latest observation state.
    Does not write to disk — designed for live UI display.
    """

    def __init__(
        self,
        configuration: MtConnectClientConfiguration,
        interval_ms: int = 1000,
        mode: Literal["stream", "sample", "current"] = "stream",
        history_max: int = 100,
    ) -> None:
        self._configuration = configuration
        self._interval_ms = interval_ms
        self._mode = mode
        self._history_max = history_max
        self._client = MtConnectClient(configuration)

        self._probe_devices: List[MtConnectDevice] = []
        self._observations: Dict[str, MtConnectObservation] = {}
        self._history: Dict[str, List[MtConnectObservation]] = {}
        self._lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_alive = False
        self._error: Optional[str] = None

    @property
    def is_alive(self) -> bool:
        return self._is_alive

    @property
    def error(self) -> Optional[str]:
        return self._error

    def get_state(self) -> Tuple[List[MtConnectDevice], Dict[str, MtConnectObservation]]:
        """Return a snapshot of probe devices and latest observations."""
        with self._lock:
            return list(self._probe_devices), dict(self._observations)

    def get_history(self, data_item_id: str) -> List[MtConnectObservation]:
        """Return the buffered observation history for a DataItem."""
        with self._lock:
            return list(self._history.get(data_item_id, []))

    def start(self) -> "MtConnectExplorer":
        if self._thread is not None:
            raise RuntimeError("MtConnectExplorer already started")
        self._stop_event.clear()
        self._is_alive = True
        self._thread = threading.Thread(
            target=self._loop,
            name="mtconnect-explorer",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def _update_observations(self, observations: List[MtConnectObservation]) -> None:
        with self._lock:
            for obs in observations:
                did = obs.attributes.get("dataItemId", "")
                if not did:
                    continue
                self._observations[did] = obs
                hist = self._history.setdefault(did, [])
                hist.append(obs)
                if len(hist) > self._history_max:
                    del hist[: -self._history_max]

    def _loop(self) -> None:
        reader = None
        try:
            logger.debug("MtConnectExplorer thread started")

            probe_frame = self._client.probe()
            with self._lock:
                self._probe_devices = probe_frame.parse_probe_devices()
            logger.debug("MtConnectExplorer: probe fetched")

            current_frame = self._client.current()
            next_sequence = current_frame.parse_stream_next_sequence()
            self._update_observations(current_frame.parse_observations())
            logger.debug("MtConnectExplorer: current fetched")

            match self._mode:
                case "stream":
                    stream_frame = self._client.sample_stream(
                        from_sequence=next_sequence,
                        interval=self._interval_ms,
                    )
                    if stream_frame.reader is None:
                        raise Exception(f"Failed to start stream: {stream_frame.frame.exception or 'Unknown error'}")
                    reader = stream_frame.reader
                    logger.debug("MtConnectExplorer: streaming started")
                    while reader.is_alive:
                        if self._stop_event.is_set():
                            break
                        frames = reader.drain()
                        if not frames:
                            time.sleep(0.05)
                            continue
                        for frame in frames:
                            self._update_observations(frame.parse_observations())

                case "sample":
                    next_t = time.time() + self._interval_ms / 1000.0
                    while not self._stop_event.is_set():
                        if time.time() < next_t:
                            time.sleep(0.001)
                            continue
                        next_t = time.time() + self._interval_ms / 1000.0
                        frame = self._client.sample(from_sequence=next_sequence)
                        next_sequence = frame.parse_stream_next_sequence()
                        self._update_observations(frame.parse_observations())

                case "current":
                    next_t = time.time() + self._interval_ms / 1000.0
                    while not self._stop_event.is_set():
                        if time.time() < next_t:
                            time.sleep(0.001)
                            continue
                        next_t = time.time() + self._interval_ms / 1000.0
                        frame = self._client.current()
                        self._update_observations(frame.parse_observations())

        except Exception as ex:
            self._error = str(ex)
            logger.error(f"MtConnectExplorer thread exception: {ex}")
        finally:
            if reader is not None:
                reader.stop()
            self._is_alive = False
            logger.debug("MtConnectExplorer thread exited")
