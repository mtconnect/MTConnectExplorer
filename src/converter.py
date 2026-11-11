import os
import json
import unicodedata
import string
import logging
import threading
import time
from typing import Dict, Set, Tuple, List, Optional
from datetime import datetime, timezone, timedelta
from pydantic import ValidationError
from pathlib import Path

from .client import MtConnectClientFrame, MtConnectObservation
from .dataset import DataFrame, DatasetWriter, DatasetReader
#from .shdr import *
from .shdr import (
    ShdrConditionLevel,
    ShdrObservation,
    ShdrDataItem,
    ShdrCondition,
    ShdrDataSet,
    ShdrTable,
    ShdrTimeSeries,
    ShdrAsset,
    ShdrRemoveAsset,
    ShdrRemoveAllAssets,
    ShdrAdapterConfiguration,
    ShdrAdapter,
)

logger = logging.getLogger(__name__)


class XmlToShdrConverter:

    @staticmethod
    def get_device_ids(input_path: str) -> Set[str]:
        if not isinstance(input_path, str):
            raise TypeError("path must be a str")
        if not os.path.exists(input_path):
            raise FileExistsError(f"file '{input_path}' does not exist.")
        if not os.path.isfile(input_path):
            raise FileExistsError(f"file '{input_path}' is not a file.")
        
        reader = DatasetReader("RawMTConnect-v0.1")
        reader.open(input_path)

        # Read the probe response and read devices
        data_frame = reader.read()
        mtc_frame = MtConnectClientFrame.model_validate(data_frame.payload)
        if mtc_frame.type != "probe":
            raise Exception("First frame must be probe request/response")
        devices = mtc_frame.parse_probe_devices()
        return {device.id for device in devices}
    
    @staticmethod
    def get_device_names(input_path: str) -> Set[str]:
        if not isinstance(input_path, str):
            raise TypeError("path must be a str")
        if not os.path.exists(input_path):
            raise FileExistsError(f"file '{input_path}' does not exist.")
        if not os.path.isfile(input_path):
            raise FileExistsError(f"file '{input_path}' is not a file.")
        
        reader = DatasetReader("RawMTConnect-v0.1")
        reader.open(input_path)

        # Read the probe response and read devices
        data_frame = reader.read()
        mtc_frame = MtConnectClientFrame.model_validate(data_frame.payload)
        if mtc_frame.type != "probe":
            raise Exception("First frame must be probe request/response")
        devices = mtc_frame.parse_probe_devices()
        return {device.name for device in devices}

    @staticmethod
    def parse_probe_device_model_by_id(frame: MtConnectClientFrame, device_id: str) -> str:
        if not isinstance(frame, MtConnectClientFrame):
            raise TypeError("frame must be a MtConnectClientFrame")
        if not isinstance(device_id, str):
            raise TypeError("device_id must be a str")
        
        if frame.type != "probe":
            raise Exception("First frame must be probe request/response")
        
        devices = frame.parse_probe_devices()
        device = [device for device in devices if device.id == device_id]
        if len(device) == 0:
            raise Exception("No device found for the provided device ID")
        return device[0].model
    
    @staticmethod
    def parse_probe_device_model_by_name(frame: MtConnectClientFrame, device_name: str) -> str:
        if not isinstance(frame, MtConnectClientFrame):
            raise TypeError("frame must be a MtConnectClientFrame")
        if not isinstance(device_name, str):
            raise TypeError("device_name must be a str")
        
        if frame.type != "probe":
            raise Exception("First frame must be probe request/response")
        
        devices = frame.parse_probe_devices()
        device = [device for device in devices if device.name == device_name]
        if len(device) == 0:
            raise Exception("No device found for the provided device name")
        return device[0].model
        
    @staticmethod
    def convert_file(input_path: str, output_path: str, device_id: str = None):
        if not isinstance(input_path, str):
            raise TypeError("path must be a str")
        if not os.path.exists(input_path):
            raise FileExistsError(f"file '{input_path}' does not exist.")
        if not os.path.isfile(input_path):
            raise FileExistsError(f"file '{input_path}' is not a file.")
        
        if not isinstance(output_path, str):
            raise TypeError("output_path must be a str")
        
        if device_id is not None:
            if not isinstance(device_id, str):
                raise TypeError("device_id must be a str")
        
        reader = DatasetReader("RawMTConnect-v0.1")
        reader.open(input_path)
        # writer = DatasetWriter("Shdr-v0.1")
        # writer.open(output_path)

        # Read the probe response and save its device XML as the payload
        data_frame = reader.read()
        mtc_frame = MtConnectClientFrame.model_validate(data_frame.payload)
        if mtc_frame.type != "probe":
            raise Exception("First frame must be probe request/response")
        devices = mtc_frame.parse_probe_devices()

        # Create writers. Dict[id, DatasetWriter]
        writers: Dict[str, DatasetWriter] = {}
        if device_id is not None:
            writers[device_id] = DatasetWriter(output_path)
        else:
            for device in devices:
                # Add id to end of filename
                _writer_file = Path(output_path).stem + f"-{device.id}" + Path(output_path).suffix
                # Clean filename
                _writer_file = unicodedata.normalize('NFKD', _writer_file).encode('ASCII', 'ignore').decode()
                valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
                _writer_file = "".join(c for c in _writer_file if c in valid_chars)
                # Create writer
                _writer_path = os.path.join(Path(output_path).parent, _writer_file)
                writers[device.id] = DatasetWriter("Shdr-v0.1").open(_writer_path)
        # Write probe reponses to file
        for device in devices:
            writers[device.id].write(DataFrame(
                timestamp=data_frame.timestamp,
                id=device.uuid,
                #id=device.id,
                #id="Devices.xml",
                payload=device.model,
            ))
        
        device_by_uuid = {d.uuid: d for d in devices}
        while not reader.is_finished:
            data_frame = reader.read()
            mtc_frame = MtConnectClientFrame.model_validate(data_frame.payload)
            if mtc_frame.type.lower() not in ["current", "sample"]:
                raise Exception("Second frame and beyond must be current or sample request/response")
            mtc_streams = mtc_frame.parse_device_streams()
            for mtc_stream in mtc_streams:
                writer = writers[device_by_uuid[mtc_stream.uuid].id]
                for mtc_obs in mtc_stream.observations:
                    try:
                        shdr_obs = XmlToShdrConverter.parse_observation(mtc_obs)
                    except ValidationError as ex:
                        errors = [e for e in ex.errors()]
                        logger.error(f"Validation Error: {ex.title}\n{ex.json(indent=2)}\n{mtc_obs.model_dump_json(indent=2)}\n")
                        continue
                    except Exception as ex:
                        logger.error(f"Error parsing observation: {ex}\n{mtc_obs.model_dump_json(indent=2)}")
                        continue

                    writer.write(DataFrame(
                        timestamp=data_frame.timestamp,
                        id=shdr_obs.__class__.__name__,
                        payload=shdr_obs.model_dump()
                    ))

        writer.close()
        logger.debug("Conversion Complete")

    @staticmethod
    def parse_observation(obs: MtConnectObservation) -> ShdrObservation:
        if not isinstance(obs, MtConnectObservation):
            raise TypeError("obs must be a MtConnectObservation")

        dataitem_id = obs.attributes.get("dataItemId")
        category = obs.attributes.get("category", "")
        representation = obs.attributes.get("representation", "VALUE")
        name = obs.attributes.get("name")
        asset_id = obs.attributes.get("assetId")
        asset_type = obs.attributes.get("assetType")
        #_ts = obs.attributes.get("timestamp")
        #timestamp = datetime.fromisoformat(_ts) if _ts else None
        timestamp = obs.timestamp

        if name is None and dataitem_id is not None:
            name = dataitem_id

        if asset_id is not None or asset_type is not None:
            #if asset_id is None: asset_id = ""
            #if asset_type is None: asset_type = ""
            if asset_id is None and name is not None:
                asset_id = name

            # obs.tag == AssetAdded, AssetChanged, AssetRemoved

            if obs.tag == "AssetAdded":
                return ShdrAsset(
                    timestamp=timestamp,
                    asset_id=asset_id,
                    asset_type=obs.tag,
                    content=obs.text,
                )
            
            if obs.tag == "AssetRemoved":
            #if obs.attributes.get("removed") == "true":
                return ShdrRemoveAsset(
                    timestamp=timestamp,
                    asset_id=asset_id,
                )
            
            if obs.tag == "AssetChanged":
                # return ShdrRemoveAsset(
                #     timestamp=timestamp,
                #     asset_id=asset_id,
                # )
                return ShdrAsset(
                    timestamp=timestamp,
                    asset_id=asset_id,
                    asset_type=obs.tag,
                    content=obs.text,
                )
            
            raise Exception(f"Unknown asset tag: {obs.tag}")
            
            

        if category == "CONDITION":
            return ShdrCondition(
                timestamp=timestamp,
                name=name,
                level=ShdrConditionLevel(obs.tag.upper()),
                native_code=obs.attributes.get("nativeCode", ""),
                native_severity=obs.attributes.get("nativeSeverity", ""),
                qualifier=obs.attributes.get("qualifier", ""),
                message=obs.text or "",
            )
        
        if representation == "TIME_SERIES":
            sample_rate = obs.attributes.get("sampleRate")
            return ShdrTimeSeries(
                timestamp=timestamp,
                name=name,
                values=[float(v) for v in obs.text.split()] if obs.text else [],
                frequency=float(sample_rate) if sample_rate is not None else None,
            )
        
        # DATA_SET / TABLE observations are identified by their <Entry>/<Cell>
        # structure: MTConnect streams carry the entries directly and do NOT repeat
        # the `representation` attribute, so we detect by structure (and honour the
        # attribute too, on the chance an agent does include it).
        if obs.entries or representation in ("DATA_SET", "TABLE"):
            # No entries + UNAVAILABLE text → emit a plain UNAVAILABLE.
            if not obs.entries and (obs.text or "").strip().upper() == "UNAVAILABLE":
                return ShdrDataItem(timestamp=timestamp, name=name, value=None)

            reset_triggered = obs.attributes.get("resetTriggered")

            # A TABLE has <Cell> children inside its entries; a DATA_SET does not.
            if representation == "TABLE" or any(entry.cells for entry in obs.entries):
                # A removed entry becomes an empty row, rendered as a key-only removal.
                rows = {
                    entry.key: (
                        {} if entry.removed
                        else {cell.key: cell.text for cell in entry.cells}
                    )
                    for entry in obs.entries
                }
                return ShdrTable(
                    timestamp=timestamp,
                    name=name,
                    rows=rows,
                    reset_triggered=reset_triggered,
                )

            # DATA_SET: each <Entry key="...">value</Entry>; a removed entry becomes
            # None, which the SHDR data set renders as a key-only removal.
            entries = {
                entry.key: (None if entry.removed else entry.text)
                for entry in obs.entries
            }
            return ShdrDataSet(
                timestamp=timestamp,
                name=name,
                entries=entries,
                reset_triggered=reset_triggered,
            )
        
        return ShdrDataItem(
            timestamp=timestamp,
            name=name,
            value=obs.text,
        )


class MtConnectDatasetReader:

    def __init__(self, device_name: str):
        if not isinstance(device_name, str):
            raise Exception("device_name must be a str")
        self._device_name = device_name

        self._reader = DatasetReader("RawMTConnect-v0.1")

    @property
    def is_finished(self) -> bool:
        return self._reader.is_finished

    @property
    def next_offset(self) -> Optional[int]:
        """Byte offset of the next frame to read (for the seek keyframe index)."""
        return self._reader.next_offset

    def open(self, path: str) -> "MtConnectDatasetReader":
        self._reader.open(path)
        return self

    def open_at(self, path: str, offset: int) -> "MtConnectDatasetReader":
        """Open positioned at a specific frame byte offset (an observation frame,
        not the probe) — used to resume delta replay from an indexed snapshot."""
        self._reader.open_at(path, offset)
        return self

    def close(self) -> None:
        if self._reader.is_open:
            self._reader.close()

    def read_probe_device_model(self) -> str:
        data_frame = self._reader.read()
        mtc_frame = MtConnectClientFrame.model_validate(data_frame.payload)
        if mtc_frame.type != "probe":
            raise Exception("Invalid data frame type")
        return XmlToShdrConverter.parse_probe_device_model_by_name(mtc_frame, self._device_name)

    def read(self) -> Tuple[datetime, List[ShdrObservation]]:
        data_frame = self._reader.read()
        mtc_frame = MtConnectClientFrame.model_validate(data_frame.payload)
        mtc_obs: Optional[List[MtConnectObservation]] = None
        for stream in mtc_frame.parse_device_streams():
            if stream.name == self._device_name:
                mtc_obs = stream.observations
                break
        if mtc_obs is None:
            # There are no observations for this device in this stream
            return data_frame.timestamp, []
            raise Exception("No observation found for device name: " + self._device_name)
        
        shdr_obs = [XmlToShdrConverter.parse_observation(o) for o in mtc_obs]
        return data_frame.timestamp, shdr_obs
    

def dataset_duration(file_path: str) -> float:
    """Total playback span, in seconds, of a RawMTConnect-v0.1 dataset — from the
    first observation frame (the frame after the leading probe) to the last frame.

    This matches the span the player replays in one pass (``pass_frame_start`` in
    ``_loop`` is the first post-probe frame). Returns 0.0 for an empty or
    probe-only dataset. The reader is forward-only with no index, so duration is
    not known during playback; this pre-scans the file, but parses only the two
    boundary lines' timestamps (not every frame's payload), so it stays cheap on
    large files."""
    def _ts(line: str) -> datetime:
        return datetime.fromisoformat(json.loads(line)["TIMESTAMP"])

    with open(file_path, "r") as f:
        f.readline()                    # header
        f.readline()                    # probe frame (first data line) — skipped
        start_line = f.readline()       # first observation frame = pass start
        if not start_line.strip():
            return 0.0
        last_line = start_line
        for line in f:
            if line.strip():
                last_line = line
    return max((_ts(last_line) - _ts(start_line)).total_seconds(), 0.0)


class _DeviceStateFold:
    """Accumulates a single device's full observation state so it can be re-emitted
    as a ``/current``-equivalent snapshot. Used to build the seek keyframe index:
    replaying every observation from the start folds into this, and a snapshot taken
    at time T is the exact device state at T (SHDR observations that, emitted to a
    fresh/uncertain agent, reconstruct that state).

    Cumulative semantics mirror the agent: values/time-series are last-wins;
    conditions keep the active set per native code (a code-less NORMAL clears all);
    DATA_SET/TABLE accumulate with reset + per-entry delete; assets add/remove."""

    def __init__(self) -> None:
        self.values: Dict[str, ShdrDataItem] = {}
        self.time_series: Dict[str, ShdrTimeSeries] = {}
        # name -> {native_code (""=no code) -> condition}. Empty dict == all NORMAL.
        self.conditions: Dict[str, Dict[str, ShdrCondition]] = {}
        self.data_sets: Dict[str, Dict[str, Any]] = {}
        self.tables: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.assets: Dict[str, ShdrAsset] = {}

    def apply(self, obs: ShdrObservation) -> None:
        if isinstance(obs, ShdrDataItem):
            self.values[obs.name] = obs
        elif isinstance(obs, ShdrTimeSeries):
            self.time_series[obs.name] = obs
        elif isinstance(obs, ShdrCondition):
            codes = self.conditions.setdefault(obs.name, {})
            code = obs.native_code or ""
            if obs.level == ShdrConditionLevel.NORMAL:
                if code:
                    codes.pop(code, None)  # NORMAL for a code clears that code
                else:
                    codes.clear()          # code-less NORMAL clears all
            elif obs.level == ShdrConditionLevel.UNAVAILABLE:
                codes.clear()
                codes["\x00unavail"] = obs
            else:  # WARNING / FAULT
                codes.pop("\x00unavail", None)
                codes[code] = obs
        elif isinstance(obs, ShdrDataSet):
            cur = self.data_sets.setdefault(obs.name, {})
            if obs.reset_triggered:
                cur.clear()
            for k, v in obs.entries.items():
                if v is None or v == "":
                    cur.pop(k, None)
                else:
                    cur[k] = v
        elif isinstance(obs, ShdrTable):
            cur = self.tables.setdefault(obs.name, {})
            if obs.reset_triggered:
                cur.clear()
            for rk, cells in obs.rows.items():
                if not cells:
                    cur.pop(rk, None)
                else:
                    cur[rk] = dict(cells)
        elif isinstance(obs, ShdrAsset):
            self.assets[obs.asset_id] = obs
        elif isinstance(obs, ShdrRemoveAsset):
            self.assets.pop(obs.asset_id, None)
        elif isinstance(obs, ShdrRemoveAllAssets):
            self.assets = {
                aid: a for aid, a in self.assets.items() if a.asset_type != obs.asset_type
            }

    def snapshot(self) -> List[ShdrObservation]:
        """A fresh list of timestamp-less observations that reconstructs the current
        state when emitted (timestamps are assigned by the player at emit time)."""
        out: List[ShdrObservation] = []
        for v in self.values.values():
            out.append(v.model_copy(update={"timestamp": None}))
        for ts in self.time_series.values():
            out.append(ts.model_copy(update={"timestamp": None}))
        for name, codes in self.conditions.items():
            # Clear any stale agent state for this condition first, then set actives.
            out.append(ShdrCondition(name=name, level=ShdrConditionLevel.NORMAL))
            for c in codes.values():
                out.append(c.model_copy(update={"timestamp": None}))
        for name, entries in self.data_sets.items():
            out.append(ShdrDataSet(name=name, entries=dict(entries), reset_triggered="MANUAL"))
        for name, rows in self.tables.items():
            out.append(ShdrTable(
                name=name,
                rows={rk: dict(cells) for rk, cells in rows.items()},
                reset_triggered="MANUAL",
            ))
        for a in self.assets.values():
            out.append(a.model_copy(update={"timestamp": None}))
        return out


class MtConnectDatasetPlayer:

    def __init__(self, host: str = "127.0.0.1", port: int = 7878, re_timestamp: bool = True, loop: bool = True, thread_name: str = "dataset-player", snapshot_interval_s: float = 15.0):
        if not isinstance(host, str):
            raise TypeError("host must be a str")
        self._host = host

        if not isinstance(port, int):
            raise TypeError("port must be an int")
        self._port = port

        if not isinstance(thread_name, str):
            raise TypeError("thread_name must be a str")
        # Name applied to the playback thread; callers may encode a session label
        # here so the thread's log records can be attributed to a playback.
        self._thread_name = thread_name

        if not isinstance(re_timestamp, bool):
            raise TypeError("re_timestamp must be a bool")
        # When True, recorded observation timestamps are replaced on emit with a
        # time offset from the play start equal to the frame's original offset
        # from the first frame (data appears "live"). When False, original
        # timestamps are kept.
        self._re_timestamp = re_timestamp

        if not isinstance(loop, bool):
            raise TypeError("loop must be a bool")
        # When True, playback repeats from the start once the dataset is
        # exhausted. The device-model (probe) line is only consumed on the first
        # pass; subsequent passes skip it and replay observations only.
        # NB: not named ``self._loop`` so it does not shadow the _loop() method.
        self._loop_enabled = loop

        if not isinstance(snapshot_interval_s, (int, float)) or snapshot_interval_s <= 0:
            raise ValueError("snapshot_interval_s must be a positive number")
        # Keyframe cadence (dataset seconds): a full-state snapshot is indexed every
        # this-many seconds so a seek replays only from the nearest snapshot, not
        # from the top. Set at construction, fixed for the session.
        self._snapshot_interval_s = float(snapshot_interval_s)

        self._file_path: Optional[str] = None
        self._device_name: Optional[str] = None
        self._reader: Optional[MtConnectDatasetReader] = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_alive = False
        self._is_finished = False

        # Total dataset span (seconds), computed at open(); the scrub bar's range.
        self._duration: float = 0.0

        # Seek keyframe index, built once at open(): sorted list of
        # (snapshot_time_s, file_byte_offset, snapshot_observations). snapshot_time_s
        # is dataset in-pass time; the observations reconstruct full device state as
        # of just-before that time; file_byte_offset points at the first frame at/after
        # it (where delta replay resumes). _pass_frame_start is the dataset timestamp
        # of in-pass offset 0 (so frame_offset = frame_ts - _pass_frame_start).
        self._snapshots: List[Tuple[float, int, List[ShdrObservation]]] = []
        self._pass_frame_start: Optional[datetime] = None

        # --- Playhead (target) clock: what pause freezes and the UI reads ----------
        # The playhead advances at 1x wall-clock while playing, is frozen while
        # paused, and is set directly by a seek. Modelled as an anchor:
        #   target(now) = _pos_anchor + (now - _wall_anchor)   [playing]
        #   target      = _pos_anchor                          [paused]
        # A frame at in-pass offset O is emitted once target >= O: unpaced when
        # behind (post-seek catch-up), paced when caught up. Pause never blocks the
        # emit loop — it only stops the target advancing, so an in-flight catch-up
        # runs to the (now frozen) target and then idles.
        self._pos_anchor: float = 0.0
        self._wall_anchor: Optional[datetime] = None
        self._paused: bool = False

        # --- Emit-timestamp clock (re_timestamp): kept separate from the playhead --
        # so a backward seek/loop-wrap never emits a decreasing timestamp (the agent
        # rejects those). Emitted ts = _emit_origin + _elapsed_offset + frame_offset,
        # with a monotonic guard. _elapsed_offset accumulates completed passes.
        self._emit_origin: Optional[datetime] = None
        self._elapsed_offset: timedelta = timedelta(0)
        self._last_emit_ts: Optional[datetime] = None

        # Pending seek target (in-pass seconds); latest-wins, applied by _loop.
        self._seek_target: Optional[float] = None
        self._seek_lock = threading.Lock()

    @property
    def is_finished(self) -> bool:
        return self._is_finished

    @property
    def duration(self) -> float:
        """Total dataset span in seconds (the scrub range)."""
        return self._duration

    def _clamp(self, pos: float) -> float:
        return min(max(pos, 0.0), self._duration) if self._duration else max(0.0, pos)

    def _target_pos(self, now: Optional[datetime] = None) -> float:
        """In-pass playhead target (seconds): advances with wall-clock while
        playing, frozen while paused. Not clamped (the loop wraps at duration)."""
        if self._wall_anchor is None:
            return 0.0
        if self._paused:
            return self._pos_anchor
        now = now or datetime.now(timezone.utc)
        return self._pos_anchor + (now - self._wall_anchor).total_seconds()

    @property
    def pass_start_epoch(self) -> Optional[float]:
        """Unix time corresponding to offset 0 of the current pass, or None until
        playback has started. The UI computes the live playhead as
        ``now - pass_start_epoch`` (except while paused — see ``paused``/``position``),
        so this is republished on start, loop-wrap, seek, and pause/resume."""
        if self._wall_anchor is None:
            return None
        return self._wall_anchor.timestamp() - self._pos_anchor

    @property
    def position(self) -> float:
        """Current playhead within the pass, in seconds (clamped to [0, duration]).
        Frozen while paused (the target stops advancing)."""
        if self._wall_anchor is None:
            return 0.0
        return self._clamp(self._target_pos())

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Freeze the playhead target (no-op if already paused). Does NOT block the
        emit loop — a post-seek fast-forward keeps releasing data up to the frozen
        target and then idles. Works in any timestamp mode (pause does not rewind)."""
        with self._seek_lock:
            if self._paused:
                return
            self._pos_anchor = self._clamp(self._target_pos())
            self._wall_anchor = datetime.now(timezone.utc)
            self._paused = True

    def resume(self) -> None:
        """Resume (no-op if not paused): the target advances again from where it was."""
        with self._seek_lock:
            if not self._paused:
                return
            self._wall_anchor = datetime.now(timezone.utc)
            self._paused = False

    def seek(self, offset_seconds: float) -> None:
        """Request a seek to ``offset_seconds`` into the dataset (latest-wins).

        Applied by the playback thread: it jumps to the nearest indexed snapshot at
        or before the target, emits that full-state snapshot, then fast-forwards the
        remaining (≤ snapshot interval) deltas to the target and resumes paced
        playback. Only valid with re_timestamp on (the caller enforces this)."""
        target = self._clamp(float(offset_seconds))
        with self._seek_lock:
            self._seek_target = target

    def open(self, file_path: str, device_name: str, autoplay: bool = True) -> "MtConnectDatasetPlayer":
        if not isinstance(file_path, str):
            raise TypeError("file_path must be a str")
        
        if not isinstance(device_name, str):
            raise TypeError("device_name must be a str")
        
        if not isinstance(autoplay, bool):
            raise TypeError("autoplay must be a bool")

        if self._reader is not None or self._thread is not None:
            raise RuntimeError("Already playing")

        self._file_path = file_path
        self._device_name = device_name
        self._duration = dataset_duration(file_path)

        # Build the seek keyframe index (one pre-scan pass over this device's frames)
        # so a later scrub replays only from the nearest snapshot, not from the top.
        self._build_snapshot_index()

        self._reader = MtConnectDatasetReader(device_name).open(file_path)
        self._adapter = ShdrAdapter(
            ShdrAdapterConfiguration(
                device=device_name,
                device_model=self._reader.read_probe_device_model(),
                host=self._host,
                port=self._port,
            )
        ).start()

        if autoplay:
            self.play()
        return self

    def _build_snapshot_index(self) -> None:
        """One pre-scan pass: fold every observation into running device state and
        record a full-state snapshot every ``_snapshot_interval_s`` of dataset time.
        Each entry is (snapshot_time_s, byte_offset_of_first_frame_at/after_it,
        snapshot_observations); the snapshot is the state strictly before that frame,
        so a seek emits it then replays the ≤interval remaining deltas to the target."""
        reader = MtConnectDatasetReader(self._device_name).open(self._file_path)
        try:
            reader.read_probe_device_model()  # consume the probe/device-model frame
            fold = _DeviceStateFold()
            snapshots: List[Tuple[float, int, List[ShdrObservation]]] = []
            interval = self._snapshot_interval_s
            next_snap = interval
            pass_frame_start: Optional[datetime] = None
            while not reader.is_finished:
                byte_offset = reader.next_offset
                frame_timestamp, obs_list = reader.read()
                if pass_frame_start is None:
                    pass_frame_start = frame_timestamp
                off = (frame_timestamp - pass_frame_start).total_seconds()
                while off >= next_snap:
                    snapshots.append((next_snap, byte_offset, fold.snapshot()))
                    next_snap += interval
                for o in obs_list:
                    fold.apply(o)
            self._snapshots = snapshots
            self._pass_frame_start = pass_frame_start
            logger.debug(
                "Built %d seek snapshot(s) at %.0fs spacing for '%s'",
                len(snapshots), interval, self._device_name,
            )
        finally:
            reader.close()
    
    def play(self) -> None:
        if self._reader is None or self._adapter is None:
            raise RuntimeError("Not opened")

        self._is_alive = False
        self._is_finished = False
        self._thread = threading.Thread(
            target=self._loop,
            name=self._thread_name,
            daemon=True,
        )
        self._thread.start()


    def stop(self) -> None:
        self._stop_event.set()
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._adapter is not None:
            self._adapter.stop()
            self._adapter = None

    def _reopen_reader(self) -> "MtConnectDatasetReader":
        """Close the current reader and open a fresh one positioned at the first
        observation frame (probe discarded) — used for loop-wrap and for a seek to a
        target before the first snapshot, since the reader is forward-only."""
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
        reader = MtConnectDatasetReader(self._device_name).open(self._file_path)
        reader.read_probe_device_model()  # discard device-model (probe) line
        self._reader = reader
        return reader

    def _reopen_reader_at(self, byte_offset: int) -> "MtConnectDatasetReader":
        """Reopen the reader positioned at an indexed frame byte offset (a snapshot's
        resume point), skipping everything before it."""
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:
                pass
        self._reader = MtConnectDatasetReader(self._device_name).open_at(
            self._file_path, byte_offset
        )
        return self._reader

    def _emit_ts(self, frame_offset: float) -> datetime:
        """Next emit timestamp (re_timestamp mode): emit_origin + elapsed + offset,
        with a strictly-increasing guard so a backward seek/loop-wrap never emits a
        decreasing timestamp (the agent rejects those)."""
        ts = self._emit_origin + self._elapsed_offset + timedelta(seconds=frame_offset)
        if self._last_emit_ts is not None and ts <= self._last_emit_ts:
            ts = self._last_emit_ts + timedelta(milliseconds=1)
        self._last_emit_ts = ts
        return ts

    def _emit_observations(self, obs_list: List[ShdrObservation], frame_offset: float) -> None:
        if not obs_list:
            return
        if self._re_timestamp:
            new_ts = self._emit_ts(frame_offset)
            obs_list = [o.model_copy(update={"timestamp": new_ts}) for o in obs_list]
        self._adapter.enqueue(obs_list)

    def _snapshot_at(self, target: float) -> Optional[Tuple[float, int, List[ShdrObservation]]]:
        """The indexed snapshot with the greatest time ≤ target, or None if target is
        before the first snapshot (replay from the top)."""
        chosen: Optional[Tuple[float, int, List[ShdrObservation]]] = None
        for snap in self._snapshots:
            if snap[0] <= target:
                chosen = snap
            else:
                break
        return chosen

    def _apply_seek(self, target: float) -> None:
        """Seek to in-pass offset ``target`` via the keyframe index: jump to the
        nearest snapshot ≤ target, emit its full state, and let the loop fast-forward
        the remaining (≤ interval) deltas to the target.

        Playhead: anchor so the target is 'now' (paused state preserved). Emit: rebase
        ``_emit_origin`` so the first re-emitted timestamp is just past the last one,
        keeping timestamps strictly increasing across a backward jump."""
        snap = self._snapshot_at(target)
        self._pos_anchor = self._clamp(target)
        self._wall_anchor = datetime.now(timezone.utc)
        if snap is not None:
            snap_time, byte_offset, snap_obs = snap
            self._reopen_reader_at(byte_offset)
            emit_base = snap_time
        else:
            self._reopen_reader()  # target precedes the first snapshot → from top
            emit_base = 0.0
            snap_obs = None
        if self._last_emit_ts is not None:
            self._emit_origin = (
                self._last_emit_ts + timedelta(milliseconds=1)
                - self._elapsed_offset - timedelta(seconds=emit_base)
            )
        if snap_obs:
            # Emit the snapshot state first so the agent is at the snapshot before the
            # ≤interval of deltas replay on top of it.
            self._emit_observations(snap_obs, emit_base)
        logger.info("Seek to %.1fs (from snapshot at %.0fs)", target, emit_base)

    def _loop(self) -> None:
        try:
            self._is_alive = True
            self._is_finished = False
            # Playhead anchored at offset 0 = now; advances at 1x while playing.
            self._pos_anchor = 0.0
            self._wall_anchor = datetime.now(timezone.utc)
            self._paused = False
            # Emit clock. On the non-seek path _emit_origin == _wall_anchor, so
            # emitted timestamps match the pre-refactor behavior (loopback test).
            self._elapsed_offset = timedelta(0)
            self._emit_origin = self._wall_anchor
            self._last_emit_ts = None
            first_pass = True
            loop_number = 1
            logger.debug("Thread started")

            while not self._stop_event.is_set():
                # First pass reuses the reader from open() (probe consumed); later
                # passes re-open from the top.
                if first_pass:
                    reader = self._reader
                    first_pass = False
                else:
                    reader = self._reopen_reader()

                if self._loop_enabled:
                    logger.info(f"Playback loop {loop_number}")

                prev_frame_timestamp: Optional[datetime] = None
                any_frames = False

                while not reader.is_finished:
                    if self._stop_event.is_set():
                        logger.debug("Thread stop received")
                        break

                    # Apply a pending seek before reading the next frame: jump to the
                    # nearest snapshot, emit its state, then fast-forward to target.
                    with self._seek_lock:
                        seek_target = self._seek_target
                        self._seek_target = None
                    if seek_target is not None:
                        self._apply_seek(seek_target)
                        reader = self._reader
                        prev_frame_timestamp = None
                        continue

                    frame_timestamp, frame_observations = reader.read()
                    any_frames = True
                    frame_offset = (frame_timestamp - self._pass_frame_start).total_seconds()

                    # Guard against an absurd GAP between consecutive frames (corrupt/
                    # misordered data) — NOT the dataset's total length.
                    max_gap_minutes = 10
                    if prev_frame_timestamp is not None:
                        gap = frame_timestamp - prev_frame_timestamp
                        if gap.total_seconds() > (max_gap_minutes * 60):
                            raise Exception(
                                f"gap between consecutive frames '{gap}' exceeds the maximum "
                                f"of '{max_gap_minutes}' minutes; the dataset may be corrupt or misordered."
                            )
                    prev_frame_timestamp = frame_timestamp

                    # Emit once the playhead target reaches this frame. Behind-target
                    # frames (a post-seek fast-forward) fall through immediately; a
                    # caught-up frame paces to real time; a paused (frozen) target
                    # holds here WITHOUT blocking any earlier catch-up. A seek
                    # interrupts so it applies at once.
                    interrupted = False
                    while frame_offset > self._target_pos():
                        if self._stop_event.is_set():
                            logger.debug("Thread stop received")
                            return
                        with self._seek_lock:
                            if self._seek_target is not None:
                                interrupted = True
                        if interrupted:
                            break
                        time.sleep(0.001)
                    if interrupted:
                        # Abandon this frame; the seek re-opens the reader, so it is
                        # simply re-read after the jump (no data lost).
                        continue

                    self._emit_observations(frame_observations, frame_offset)
                    logger.debug(f"Enqueued {len(frame_observations)} SHDR observations.")

                # Pass complete (reader exhausted) or stopped.
                if self._stop_event.is_set() or not self._loop_enabled:
                    break
                if not any_frames:
                    logger.debug("No observation frames to loop; stopping")
                    break

                # Let the playhead run out the remaining span so the pass lasts the
                # full duration (keeps the scrub bar aligned), unless a seek arrives.
                tail_seek = False
                while self._target_pos() < self._duration:
                    if self._stop_event.is_set():
                        logger.debug("Thread stop received")
                        return
                    with self._seek_lock:
                        if self._seek_target is not None:
                            tail_seek = True
                    if tail_seek:
                        break
                    time.sleep(0.02)

                if not tail_seek:
                    # Wrap: carry a full pass forward for emit-ts monotonicity and
                    # shift the playhead anchor back one duration so the next pass's
                    # target restarts near 0.
                    self._elapsed_offset += timedelta(seconds=self._duration)
                    self._pos_anchor -= self._duration
                    loop_number += 1
                    logger.debug("Looping dataset playback")
                # else: a seek is pending — loop back, reopen, and let it apply
                # (no wrap, so the seek's anchor/emit rebase is authoritative).

            self._is_finished = True
            logger.debug("Player finished")
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
            self._is_finished = True
            self._is_alive = False
