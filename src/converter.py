import os
import unicodedata
import string
import logging
import threading
import time
from typing import Dict, Set, Tuple, List, Optional
from datetime import datetime, timezone
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
        
        if representation == "DATA_SET":
            entries = {
                entry.attrib["key"]: None if entry.attrib.get("removed") == "true" else entry.text
                for entry in el
            }
            return ShdrDataSet(
                timestamp=timestamp,
                name=name,
                entries=entries,
                reset_triggered=obs.attributes.get("resetTriggered"),
            )
        
        if representation == "TABLE":
            rows = {
                entry.attrib["key"]: {cell.attrib["key"]: cell.text for cell in entry}
                for entry in el
            }
            return ShdrTable(
                timestamp=timestamp,
                name=name,
                rows=rows,
                reset_triggered=obs.attributes.get("resetTriggered"),
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

    def open(self, path: str) -> "MtConnectDatasetReader":
        self._reader.open(path)
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
    

class MtConnectDatasetPlayer:

    def __init__(self, host: str = "127.0.0.1", port: int = 7878):
        if not isinstance(host, str):
            raise TypeError("host must be a str")
        self._host = host

        if not isinstance(port, int):
            raise TypeError("port must be an int")
        self._port = port  

        self._reader: Optional[MtConnectDatasetReader] = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_alive = False
        self._is_finished = False

    @property
    def is_finished(self) -> bool:
        return self._is_finished

    def open(self, file_path: str, device_name: str, autoplay: bool = True) -> "MtConnectDatasetPlayer":
        if not isinstance(file_path, str):
            raise TypeError("file_path must be a str")
        
        if not isinstance(device_name, str):
            raise TypeError("device_name must be a str")
        
        if not isinstance(autoplay, bool):
            raise TypeError("autoplay must be a bool")

        if self._reader is not None or self._thread is not None:
            raise RuntimeError("Already playing")
        
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
    
    def play(self) -> None:
        if self._reader is None or self._adapter is None:
            raise RuntimeError("Not opened")
        
        self._is_alive = False
        self._is_finished = False
        self._thread = threading.Thread(
            target=self._loop,
            name="dataset-player",
            daemon=True,
        )
        self._thread.start()


    def stop(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self._adapter is not None:
            self._adapter.stop()
            self._adapter = None

    def _loop(self) -> None:
        try:
            self._is_alive = True
            self._is_finished = False
            start = datetime.now(timezone.utc)
            frame_start: Optional[datetime] = None
            logger.debug("Thread started")

            while not self._reader.is_finished:
                if self._stop_event.is_set():
                    logger.debug("Thread stop received")
                    break

                # Get the next frame and determine when it should be played
                delta = datetime.now(timezone.utc) - start
                frame_timestamp, frame_observations = self._reader.read()
                if frame_start is None:
                    frame_start = frame_timestamp
                frame_delta = frame_timestamp - frame_start
                
                if frame_delta.total_seconds() > 120: # 2 minutes
                    raise Exception(f"Invalid frame_delta: {frame_delta}")

                # Wait for frame time
                while frame_delta > delta:
                    if self._stop_event.is_set():
                        logger.debug("Thread stop received")
                        return
                    time.sleep(0.001)
                    delta = datetime.now(timezone.utc) - start

                self._adapter.enqueue(frame_observations)
                logger.debug(f"Enqueued {len(frame_observations)} SHDR observations.")

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
