"""
Headless SHDR-log -> dataset converter (CLI utility, wired into ``src/main.py``
via ``--convert``).

Converts a raw MTConnect **SHDR** adapter log (``timestamp|Device:dataItem|value``
lines) plus the matching ``Devices.xml`` into a ``RawMTConnect-v0.1`` dataset —
the same format the Capture page writes and the Play page replays. No live agent
is involved: the ``Devices.xml`` becomes the probe request/response, and the log
lines are grouped into time-windowed ``sample`` request/responses whose
``<MTConnectStreams>`` XML is synthesized directly.

The synthesized observation elements are round-trippable by the exact code the
Play page uses: ``MtConnectClientFrame.parse_device_streams`` collects any element
carrying a ``dataItemId`` attribute, and ``XmlToShdrConverter.parse_observation``
keys off the ``name`` and ``category`` attributes — so every observation element
carries ``dataItemId``, ``name``, ``category``, ``timestamp`` and ``sequence``.
"""

import logging
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .client import MtConnectClientFrame
from .dataset import DataFrame, DatasetWriter

logger = logging.getLogger(__name__)

_DATASET_HEADER = "RawMTConnect-v0.1"
_STREAMS_NS = "urn:mtconnect.org:MTConnectStreams:2.7"


@dataclass(slots=True)
class _DataItemMeta:
    id: str
    name: str
    category: str          # SAMPLE | EVENT | CONDITION
    type: str
    element: str           # observation element tag (PascalCase of the type)


@dataclass(slots=True)
class _Obs:
    ts: datetime
    device: str
    name: str
    meta: _DataItemMeta
    value: Optional[str] = None          # data (VALUE) observations
    is_condition: bool = False           # CONDITION observations
    level: str = "Normal"
    native_code: str = ""
    native_severity: str = ""
    qualifier: str = ""
    message: str = ""


def _local(tag: str) -> str:
    """Local (namespace-stripped) XML tag name."""
    return tag.split("}", 1)[-1]


def _pascal_case(mtc_type: str) -> str:
    """MTConnect type (SNAKE_CASE) -> observation element name (PascalCase),
    e.g. ``PATH_POSITION`` -> ``PathPosition``. The element tag is not consulted
    on replay, but a faithful name keeps the dataset indistinguishable from a
    real capture."""
    return "".join(part.capitalize() for part in mtc_type.split("_")) or "Event"


def _fmt_ts(dt: datetime) -> str:
    """UTC ISO-8601 with a trailing ``Z`` (microsecond precision)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_ts(text: str) -> datetime:
    """Parse an SHDR ISO-8601 UTC timestamp (variable fractional precision, ``Z``)."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def build_device_index(devices_xml: str) -> Tuple[Dict[str, Dict[str, _DataItemMeta]], List[Tuple[str, str]]]:
    """Parse a ``Devices.xml`` string into ``{device_name: {dataitem_name: meta}}``
    plus an ordered ``[(name, uuid), ...]`` list. Log keys map to DataItem ``name``
    and the key prefix to the Device ``name``/``uuid``."""
    root = ET.fromstring(devices_xml)
    index: Dict[str, Dict[str, _DataItemMeta]] = {}
    devices: List[Tuple[str, str]] = []
    for dev_el in root.iter():
        if _local(dev_el.tag) != "Device":
            continue
        dname = dev_el.attrib.get("name") or dev_el.attrib.get("uuid") or ""
        duuid = dev_el.attrib.get("uuid") or dname
        devices.append((dname, duuid))
        items: Dict[str, _DataItemMeta] = {}
        for di in dev_el.iter():
            if _local(di.tag) != "DataItem":
                continue
            a = di.attrib
            nm = a.get("name")
            if not nm:
                continue
            typ = a.get("type", "")
            items[nm] = _DataItemMeta(
                id=a.get("id", nm),
                name=nm,
                category=a.get("category", ""),
                type=typ,
                element=_pascal_case(typ),
            )
        index[dname] = items
    return index, devices


def parse_log(log_path: str, index: Dict[str, Dict[str, _DataItemMeta]]) -> Tuple[List[_Obs], int, Dict[str, int]]:
    """Stream the SHDR ``.log`` into ``_Obs`` records. Lines whose device/dataitem
    are absent from ``index`` (e.g. the agent-auto-generated ``*_asset_*`` keys)
    are skipped and counted. Returns ``(observations, total_lines, skip_counts)``."""
    obs: List[_Obs] = []
    skipped: Dict[str, int] = defaultdict(int)
    total = 0
    # Text mode collapses the file's CRLF endings; rstrip guards any stray CR.
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            total += 1
            parts = line.split("|")
            if len(parts) < 3:
                skipped["malformed"] += 1
                continue
            key = parts[1]
            if ":" not in key:
                skipped["no_device_prefix"] += 1
                continue
            device, name = key.split(":", 1)
            dev_items = index.get(device)
            if dev_items is None:
                skipped["unknown_device"] += 1
                continue
            meta = dev_items.get(name)
            if meta is None:
                skipped["unknown_dataitem"] += 1     # e.g. the *_asset_* keys
                continue
            try:
                ts = _parse_ts(parts[0])
            except ValueError:
                skipped["bad_timestamp"] += 1
                continue

            if meta.category == "CONDITION":
                # ts|key|level|nativeCode|nativeSeverity|qualifier|message
                fields = parts[2:] + [""] * 5
                obs.append(_Obs(
                    ts=ts, device=device, name=name, meta=meta, is_condition=True,
                    level=(fields[0] or "Normal").strip().capitalize(),
                    native_code=fields[1], native_severity=fields[2],
                    qualifier=fields[3], message=fields[4],
                ))
            else:
                # A value never contains a pipe here, but join defensively.
                obs.append(_Obs(ts=ts, device=device, name=name, meta=meta, value="|".join(parts[2:])))
    return obs, total, dict(skipped)


def _build_streams_xml(window: List[_Obs], seq: List[int]) -> str:
    """Synthesize one ``<MTConnectStreams>`` document for a window of observations,
    grouped into one ``<DeviceStream>`` per device. ``seq`` is a single-element
    list used as a mutable global sequence counter."""
    root = ET.Element("MTConnectStreams")
    root.set("xmlns", _STREAMS_NS)
    header = ET.SubElement(root, "Header")
    header.set("creationTime", _fmt_ts(window[-1].ts))
    header.set("sender", "shdr-log-converter")
    header.set("instanceId", "1")
    header.set("version", "2.7")
    header.set("bufferSize", "131072")
    streams = ET.SubElement(root, "Streams")

    by_device: Dict[str, List[_Obs]] = defaultdict(list)
    for o in window:
        by_device[o.device].append(o)

    for device, items in by_device.items():
        ds = ET.SubElement(streams, "DeviceStream", {"name": device, "uuid": device})
        cs = ET.SubElement(ds, "ComponentStream",
                           {"component": "Device", "name": device, "componentId": device})
        for o in items:
            seq[0] += 1
            tag = o.level if o.is_condition else o.meta.element
            el = ET.SubElement(cs, tag)
            el.set("dataItemId", o.meta.id)
            el.set("name", o.name)
            el.set("category", o.meta.category)
            el.set("timestamp", _fmt_ts(o.ts))
            el.set("sequence", str(seq[0]))
            if o.is_condition:
                if o.meta.type:
                    el.set("type", o.meta.type)
                if o.native_code:
                    el.set("nativeCode", o.native_code)
                if o.native_severity:
                    el.set("nativeSeverity", o.native_severity)
                if o.qualifier:
                    el.set("qualifier", o.qualifier)
                if o.message:
                    el.text = o.message
            else:
                el.text = o.value
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def _default_out_path(log_path: str, device: Optional[str]) -> str:
    stem = Path(log_path).stem
    if device:
        stem = f"{stem}-{device}"
    return str(Path("datasets") / f"{stem}.jsonl")


def convert_log(
    log_path: str,
    devices_path: str,
    out_path: Optional[str] = None,
    *,
    sample_interval_ms: int = 250,
    device: Optional[str] = None,
) -> str:
    """Convert an SHDR ``.log`` + ``Devices.xml`` into a ``RawMTConnect-v0.1``
    dataset. Emits one probe frame (from ``Devices.xml``) followed by one
    ``sample`` frame per ``sample_interval_ms`` window of observations. When
    ``device`` is given, only that device is included. Returns the output path."""
    if not Path(log_path).is_file():
        raise FileNotFoundError(f"log file not found: {log_path}")
    if not Path(devices_path).is_file():
        raise FileNotFoundError(f"devices file not found: {devices_path}")

    devices_xml = Path(devices_path).read_text(encoding="utf-8")
    index, _devices = build_device_index(devices_xml)
    if device is not None:
        if device not in index:
            raise ValueError(f"device '{device}' not in {devices_path}; available: {sorted(index)}")
        index = {device: index[device]}   # restrict; parse_log then skips other devices

    logger.info("Parsing SHDR log: %s", log_path)
    observations, total, skipped = parse_log(log_path, index)
    if not observations:
        raise ValueError("no convertible observations found in the log (nothing matched Devices.xml)")
    # Input timestamps are not strictly monotonic (interleaved device streams);
    # sort so sample frames advance in time for the Play page's paced replay.
    observations.sort(key=lambda o: o.ts)

    if out_path is None:
        out_path = _default_out_path(log_path, device)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    span = observations[-1].ts - observations[0].ts
    interval = timedelta(milliseconds=sample_interval_ms)
    seq = [0]
    devices_seen = set()
    frames = 0

    writer = DatasetWriter(_DATASET_HEADER)
    writer.open(out_path)
    try:
        # Frame 1: probe request/response (Devices.xml verbatim).
        writer.write(DataFrame(
            timestamp=observations[0].ts,
            id="MtConnectClientFrame",
            payload=MtConnectClientFrame(
                timestamp=observations[0].ts,
                type="probe",
                parameters={"device": device},
                url=f"shdr-log://{Path(devices_path).name}/probe",
                response=devices_xml,
            ),
        ))
        # Frames 2..N: one sample per time window.
        i, n = 0, len(observations)
        while i < n:
            window_end = observations[i].ts + interval
            j = i
            while j < n and observations[j].ts < window_end:
                devices_seen.add(observations[j].device)
                j += 1
            window = observations[i:j]
            frame_ts = window[-1].ts
            writer.write(DataFrame(
                timestamp=frame_ts,
                id="MtConnectClientFrame",
                payload=MtConnectClientFrame(
                    timestamp=frame_ts,
                    type="sample",
                    parameters={"device": device, "interval": sample_interval_ms},
                    url="shdr-log://sample",
                    response=_build_streams_xml(window, seq),
                ),
            ))
            frames += 1
            i = j
    finally:
        writer.close()

    summary = (
        f"Converted '{log_path}' -> '{out_path}'\n"
        f"  {total:,} log lines read, {len(observations):,} observations kept, "
        f"{frames:,} sample frames, {len(devices_seen)} device(s): {', '.join(sorted(devices_seen))}\n"
        f"  span {span.total_seconds():.1f}s, sample window {sample_interval_ms}ms"
    )
    if skipped:
        summary += "\n  skipped lines: " + ", ".join(f"{k}={v}" for k, v in sorted(skipped.items()))
    logger.info(summary.replace("\n", " | "))
    print(summary)
    return out_path
