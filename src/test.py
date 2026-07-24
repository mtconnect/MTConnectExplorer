import math
import time
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from textwrap import dedent
from typing import Any, List, Literal
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from .dataset import DatasetReader, DatasetWriter
from .shdr import ShdrAdapter, ShdrDataItem, ShdrAdapterConfiguration, ShdrDataPlayer, ShdrObservation, get_observation_type
from .client import MtConnectClient, MtConnectDataCollector, MtConnectDataCollectorConfiguration, MtConnectClientConfiguration
from .converter import MtConnectDatasetPlayer, XmlToShdrConverter
from .agent import Agent, AgentConfig, AgentAdapter
from .dataset import DataFrame


# Config/logging bootstrap consolidated in src/services/config.py (JSONC parse).
from .services.config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


# Handle signals requesting to shut down
_shutdown_flag = False
def _signal_handler(signum, frame):
    global _shutdown_flag
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    _shutdown_flag = True
def _signal_register():
    # Register signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


_device_file = dedent("""
<Device uuid="mill.001" name="Mill" id="mill.001">
  <Description model="3-Axis VMC" manufacturer="Generic">Basic 3-axis vertical machining center</Description>
  <DataItems>
    <DataItem id="avail"  category="EVENT" type="AVAILABILITY"    name="avail"/>
    <DataItem id="estop"  category="EVENT" type="EMERGENCY_STOP"  name="estop"/>
  </DataItems>
  <Components>

    <Axes id="axes" name="Axes">
      <Components>
        <Linear id="x" name="X">
          <DataItems>
            <DataItem id="Xact"  category="SAMPLE" type="POSITION" subType="ACTUAL" name="Xact"  units="MILLIMETER" nativeUnits="MILLIMETER"/>
            <DataItem id="Xload" category="SAMPLE" type="LOAD"                      name="Xload" units="PERCENT"    nativeUnits="PERCENT"/>
          </DataItems>
        </Linear>
        <Linear id="y" name="Y">
          <DataItems>
            <DataItem id="Yact"  category="SAMPLE" type="POSITION" subType="ACTUAL" name="Yact"  units="MILLIMETER" nativeUnits="MILLIMETER"/>
            <DataItem id="Yload" category="SAMPLE" type="LOAD"                      name="Yload" units="PERCENT"    nativeUnits="PERCENT"/>
          </DataItems>
        </Linear>
        <Linear id="z" name="Z">
          <DataItems>
            <DataItem id="Zact"  category="SAMPLE" type="POSITION" subType="ACTUAL" name="Zact"  units="MILLIMETER" nativeUnits="MILLIMETER"/>
            <DataItem id="Zload" category="SAMPLE" type="LOAD"                      name="Zload" units="PERCENT"    nativeUnits="PERCENT"/>
          </DataItems>
        </Linear>
        <Rotary id="c1" name="C1">
          <DataItems>
            <DataItem id="Sspeed" category="SAMPLE" type="ROTARY_VELOCITY" subType="ACTUAL" name="Sspeed" units="REVOLUTION/MINUTE" nativeUnits="REVOLUTION/MINUTE"/>
            <DataItem id="Sload"  category="SAMPLE" type="LOAD"                             name="Sload"  units="PERCENT"           nativeUnits="PERCENT"/>
            <DataItem id="Smode"  category="EVENT"  type="ROTARY_MODE"                      name="Smode">
              <Constraints><Value>SPINDLE</Value></Constraints>
            </DataItem>
          </DataItems>
        </Rotary>
      </Components>
    </Axes>

    <Controller id="ctrl" name="Controller">
      <DataItems>
        <DataItem id="cmode" category="EVENT" type="CONTROLLER_MODE" name="cmode"/>
        <DataItem id="exec"  category="EVENT" type="EXECUTION"       name="exec"/>
        <DataItem id="prog"  category="EVENT" type="PROGRAM" subType="MAIN" name="prog"/>
      </DataItems>
      <Components>
        <Path id="path" name="path">
          <DataItems>
            <DataItem id="Fact"    category="SAMPLE" type="PATH_FEEDRATE" subType="ACTUAL" name="Fact"    units="MILLIMETER/SECOND" nativeUnits="MILLIMETER/SECOND"/>
            <DataItem id="pathpos" category="SAMPLE" type="PATH_POSITION" subType="ACTUAL" name="pathpos" units="MILLIMETER_3D"      nativeUnits="MILLIMETER_3D"/>
          </DataItems>
        </Path>
      </Components>
    </Controller>

  </Components>
</Device>
""").strip()


def _loopback_generate():
    logger.info("generating loopback test data...")

    current = datetime(2000, 1, 1, tzinfo=timezone.utc)
    delta = timedelta(milliseconds=10)
    current += delta

    observations: List[ShdrObservation] = []

    last_values = {
        "avail": None,
        "estop": None,
        "cmode": None,
        "exec": None,
        "prog": None,
        "Smode": "SPINDLE", # This is the default
        "spindle": None,
        "Xact": None,
        "Yact": None,
        "Zact": None,
        "Xload": None,
        "Yload": None,
        "Zload": None,
        "Sspeed": None,
        "Sload": None,
        "Fact": None,
        "pathpos": None,
        "estop": None,
        "cmode": None,
    }
    
    def add_observation_if_changed(name: str, value: Any) -> None:
        if name not in last_values:
            raise Exception("invalid name")
        if value != last_values[name]:
            last_values[name] = value
            
            # TODO: weird behavior found in MTConnect agent.
            # If you send a float with trailing zero via SHDR,
            # the agent will read it back as an integer without the trailing zero.
            # This may cause deserialization issues on loopback where the value is deserialized as the wrong type (int instead of float)
            s_value = str(value)
            if s_value.endswith(".0"): 
                s_value = s_value[:-2]
            # May also be in the middle of a string if multiple values in a str
            if ".0 " in s_value:
                s_value = s_value.replace(".0 ", " ")
            if s_value == "":
                raise Exception("empty value")
            #observations.append(ShdrDataItem(timestamp=current, name=name, value=value))
            observations.append(ShdrDataItem(timestamp=current, name=name, value=s_value))

    add_observation_if_changed("avail", "AVAILABLE")
    add_observation_if_changed("estop", "ARMED")
    add_observation_if_changed("cmode", "AUTOMATIC")
    add_observation_if_changed("exec", "ACTIVE")
    add_observation_if_changed("prog", "TEST_PROGRAM.NC")
    add_observation_if_changed("Smode", "SPINDLE")
    current += delta

    t = 0
    for i in range(1000):
        # Simulate smooth axis motion for demonstration
        x = round(100.0 * math.sin(t), 3)
        y = round(50.0  * math.cos(t), 3)
        z = round(-25.0 + 10.0 * math.sin(t * 0.5), 3)

        #observations.extend([
        # Axis positions
        add_observation_if_changed("Xact", x)
        add_observation_if_changed("Yact", y)
        add_observation_if_changed("Zact", z),
        # ShdrDataItem(timestamp=current, name="Xact",  value=x),
        # ShdrDataItem(timestamp=current, name="Yact",  value=y),
        # ShdrDataItem(timestamp=current, name="Zact",  value=z),
        # Axis loads
        add_observation_if_changed("Xload", round(abs(math.cos(t))       * 30, 1))
        add_observation_if_changed("Yload", round(abs(math.sin(t))       * 25, 1))
        add_observation_if_changed("Zload", round(abs(math.sin(t * 0.5)) * 15, 1))
        # ShdrDataItem(timestamp=current, name="Xload", value=round(abs(math.cos(t))       * 30, 1)),
        # ShdrDataItem(timestamp=current, name="Yload", value=round(abs(math.sin(t))       * 25, 1)),
        # ShdrDataItem(timestamp=current, name="Zload", value=round(abs(math.sin(t * 0.5)) * 15, 1)),
        # Spindle
        add_observation_if_changed("Sspeed", 3000.0)
        add_observation_if_changed("Sload", round(40.0 + 10.0 * math.sin(t * 2), 1))
        # ShdrDataItem(timestamp=current, name="Sspeed", value=3000.0),
        # ShdrDataItem(timestamp=current, name="Sload",  value=round(40.0 + 10.0 * math.sin(t * 2), 1)),
        # Path
        add_observation_if_changed("Fact", 500.0)
        add_observation_if_changed("pathpos", f"{x} {y} {z}")
        # ShdrDataItem(timestamp=current, name="Fact",    value=500.0),
        # ShdrDataItem(timestamp=current, name="pathpos", value=f"{x} {y} {z}"),
        # Conditions
        add_observation_if_changed("estop", "ARMED")
        add_observation_if_changed("cmode", "AUTOMATIC")
        # ShdrDataItem(timestamp=current, name="estop",  value="ARMED"),
        # ShdrDataItem(timestamp=current, name="cmode",  value="AUTOMATIC")
        #])

        t += 0.1
        current += delta

    if not os.path.exists("test"):
        os.mkdir("test")
    writer = DatasetWriter("Shdr-v0.1")
    writer.open("test/shdr-input.jsonl")
    writer.write(DataFrame(
        timestamp=current,
        id="mill.001", # uuid
        payload=_device_file
    ))
    for observation in observations:
        writer.write(DataFrame(
            timestamp=observation.timestamp,
            id=observation.__class__.__name__,
            payload=observation,
        ))

    logger.info("loopback test data generated.")

def _loopback_run():

    logger.info("loopback test starting...")

    # Start the agent
    agent = Agent(
        AgentConfig(
            adapters=[
                AgentAdapter(
                    name="MyAdapter",
                    host="127.0.0.1",
                    port=8192,
                )
            ]
        )
    )
    agent.start()

    # Start the player/adapter but don't start playing until the agent is connected
    player = ShdrDataPlayer(
        host=agent.config.adapters[0].host,
        port=agent.config.adapters[0].port
    ).start("test/shdr-input.jsonl", autostart=False)
    logger.info("waiting for agent to connect to adapter...")
    while not player.adapter.agent_is_connected:
        time.sleep(0.001)
    logger.info("agent connected to adapter")

    # Start the data collector and wait for it to start streaming
    collector = MtConnectDataCollector(MtConnectDataCollectorConfiguration(
        configuration=MtConnectClientConfiguration(base_url="http://127.0.0.1:5000/"),
    )).start("test/xml-output.jsonl")
    while not collector.is_collecting:
        if not collector.is_alive:
            raise Exception("failed to stream")
        time.sleep(0.001)
    time.sleep(2.0)
    
    # Start playing data
    player.play()
    logger.info("loopback test started")

    while True:
        if _shutdown_flag:
            logger.info("received shutdown flag")
            break
        if not agent.is_running:
            logger.error("agent is not running")
            break
        if not player.is_alive:
            logger.error("player is not alive")
            break
        if player._is_complete:
            logger.info("player complete")
            break
        time.sleep(0.1)

    player.stop()

    logger.info("waiting for agent to disconnect from adapter...")
    while player.adapter.agent_is_connected:
        time.sleep(0.001)

    logger.info("waiting for agent to flush streams...")
    time.sleep(15.0)

    logger.info("test stopping...")

    collector.stop()

    agent.stop()

    converter = XmlToShdrConverter()
    converter.convert_file("test/xml-output.jsonl", "test/shdr-output.jsonl")

    logger.info("loopback test complete")

def _loopback_analyze():
    print()
    print("Analyzing loopback test results...")

    reader_input = DatasetReader("Shdr-v0.1")
    reader_input.open("test/shdr-input.jsonl")
    reader_output = DatasetReader("Shdr-v0.1")
    reader_output.open("test/shdr-output-mill.001.jsonl")

    # Discard probe frames
    if reader_input.read().id != "mill.001":
        raise Exception("First frame of input is not a probe")
    if reader_output.read().id != "mill.001":
        raise Exception("First frame of output is not a probe")

    obs_input: List[ShdrObservation] = []
    while not reader_input.is_finished:
        data_frame = reader_input.read()
        observation = get_observation_type(data_frame.id)(**data_frame.payload)
        obs_input.append(observation)

    obs_output: List[ShdrObservation] = []
    while not reader_output.is_finished:
        data_frame = reader_output.read()
        observation = get_observation_type(data_frame.id)(**data_frame.payload)
        obs_output.append(observation)

    print()
    print(f"Total Input Observations:  {len(obs_input)}")
    print(f"Total Output Observations: {len(obs_output)}")

    print()
    print("Identifying matching observations...")
    obs_match: List[ShdrObservation] = []
    for i in range(len(obs_input)):
        if i % 1000 == 0:
            pct = f"{float(i) / len(obs_input) * 100:.2f}"
            print(f"{pct}% Complete")
        obs1 = obs_input[i]
        if obs1 in obs_output:
            obs_match.append(obs1)
    print("Moving matching observations...")
    for obs in obs_match:
        obs_input.remove(obs)
        obs_output.remove(obs)

    print("Identifying observations created by the agent...")
    obs_agent: List[ShdrObservation] = []
    for obs in obs_output:
        if obs.timestamp > datetime(2021, 1, 1, tzinfo=timezone.utc):
            obs_agent.append(obs)
    print("Moving observations created by the agent...")
    for obs in obs_agent:
        obs_output.remove(obs)

    if len(obs_agent) > 0:
        print()
        print("Observations Created by the Agent:")
        for ob in obs_agent:
            print(str(ob.__class__.__name__).rjust(20) + " | " + str(ob.model_dump_json()))

    print()
    print(f"Matching Observations:             {len(obs_match)}")
    print(f"Unique Input Observations:         {len(obs_input)}")
    print(f"Unique Output Observations:        {len(obs_output)}")
    print(f"Observations Created by the Agent: {len(obs_agent)}")

    # Sort primarily by timestamp then by name
    obs_input.sort(key=lambda x: (x.timestamp or datetime.min, x.name))
    obs_output.sort(key=lambda x: (x.timestamp or datetime.min, x.name))

    if len(obs_input) > 0 or len(obs_output) > 0:
        print()
        print("Comparison of Unique Observations:")
        max_i = max(len(obs_input), len(obs_output))
        print("Input".rjust(86) + " | " + "Output".ljust(86))
        for i in range(max_i):
            left = ""
            right = ""
            if i < len(obs_input):
                ob = obs_input[i]
                left = str(ob.timestamp).rjust(40) + " | " + str(ob.name).rjust(15) + " | " + str(ob.value).rjust(25)
            if i < len(obs_output):
                ob = obs_output[i]
                right = str(ob.timestamp).rjust(40) + " | " + str(ob.name).rjust(15) + " | " + str(ob.value).rjust(25)
            print(f"{left.rjust(86)} | {right.ljust(86)}")
        print()
        print("Not All Observations Match. Loopback Test Failed")
        sys.exit(1)
    else:
        print()
        print("All Observations Match. Loopback Test Passed.")
        sys.exit(0)


def _mirror_test():
    logger.info("capture test starting...")

    # Start the agent
    agent = Agent(
        AgentConfig(
            adapters=[
                AgentAdapter(
                    name="MyAdapter",
                    host="127.0.0.1",
                    port=8192,
                )
            ]
        )
    ).start()

    # Probe the agent and get the Mazak device
    device_name = "Mazak" # Mazak device
    client = MtConnectClient(MtConnectClientConfiguration(
        base_url="https://demo.mtconnect.org/"
    ))
    mtc_frame = client.probe()
    mtc_device = [device for device in mtc_frame.parse_probe_devices() if device.name == device_name]
    if len(mtc_device) != 1:
        raise Exception(f"Single device with name '{device_name}' not found")
    mtc_device = mtc_device[0]

    # Create a converter and start the adapter
    adapter = ShdrAdapter(ShdrAdapterConfiguration(
        device=device_name,
        device_model=XmlToShdrConverter.parse_probe_device_model_by_id(mtc_frame, mtc_device.id),
        host = agent.config.adapters[0].host,
        port = agent.config.adapters[0].port,
    )).start()

    # Enqueue current
    for mtc_ob in client.current(device=device_name).parse_observations():
        adapter.enqueue(XmlToShdrConverter.parse_observation(mtc_ob))
    
    # Start streaming
    stream_frame = client.sample_stream(device=device_name)
    if stream_frame.reader is None:
        raise Exception(f"Failed to start stream: {stream_frame.frame.exception or 'Unknown error'}")
    reader = stream_frame.reader

    logger.info("mirror test started")

    while True:
        if _shutdown_flag:
            logger.info("received shutdown flag")
            break
        if not agent.is_running:
            logger.error("agent is not running")
            break
        if reader.count > 0:
            for mtc_ob in reader.pop().parse_observations():
                adapter.enqueue(XmlToShdrConverter.parse_observation(mtc_ob))
            continue
        time.sleep(0.001)

    logger.info("mirror test stopping...")

    adapter.stop()
    agent.stop()

    logger.info("mirror test complete")

def _play_test():
    play_file_path = "test/xml-input.jsonl"

    # If the data to play doesn't exist, create it from mtconnect demo
    if not os.path.exists(play_file_path):
        logger.info("Collecting play data...")

        collector = MtConnectDataCollector(MtConnectDataCollectorConfiguration(
            configuration=MtConnectClientConfiguration(base_url="https://demo.mtconnect.org/"),
        )).start(play_file_path)
        end_time = time.time() + 180 # 3 minutes

        while not _shutdown_flag:
            if time.time() > end_time: break
            time.sleep(0.1)

        collector.stop()
        logger.info("Collecting play data complete.")

    logger.info("play test starting...")

    # Start the agent
    agent = Agent(
        AgentConfig(
            adapters=[
                AgentAdapter(
                    name="MyAdapter",
                    host="127.0.0.1",
                    port=8192,
                )
            ]
        )
    ).start()

    # Start playing the data
    player = MtConnectDatasetPlayer(
        agent.config.adapters[0].host,
        agent.config.adapters[0].port,
    ).open(play_file_path, "Mazak")

    logger.info("play test started")

    while True:
        if _shutdown_flag:
            logger.info("received shutdown flag")
            break
        if not agent.is_running:
            logger.error("agent is not running")
            break
        if player.is_finished:
            logger.info("player finished")
            break
        time.sleep(0.001)

    logger.info("play test stopping...")

    player.stop()
    agent.stop()

    logger.info("play test complete")
    


def run_loopback_test():
    _signal_register()
    _loopback_generate()
    _loopback_run()
    _loopback_analyze()

def run_mirror_test():
    _signal_register()
    _mirror_test()

def run_play_test():
    _signal_register()
    _play_test()

if __name__ == "__main__":
    run_loopback_test()


