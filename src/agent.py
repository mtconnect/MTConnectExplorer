import io
import time
import logging
import threading
import tempfile
import shutil
import os
import subprocess
import psutil
from textwrap import dedent, indent
from typing import Annotated, Callable, List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field, PlainSerializer
from pathlib import Path


logger = logging.getLogger(__name__)
agent_logger = logging.getLogger("MTConnectAgent")


class AgentAdapter(BaseModel):
    name: str = Field(
        "MyCNC",
        description="The name of the adapter",
    )
    host: str = Field(
        "127.0.0.1", # Use this, not localhost, because IPv6 resolution causes performance issues/lag on Windows.
        description="The hostname or IP address where the adapter is running",
    )
    port: int = Field(
        7878,
        description="The TCP port where the adapter is running",
    )

    def get_adapter_config(self) -> str:
        return dedent(f"""
        {self.name} {{
            Host = {self.host}
            Port = {self.port}
        }}
        """)

# _config_template=dedent("""
# Devices = Devices.xml
# Port = 5000
# EnableSourceDeviceModels = true

# Adapters {
#   MyCNC {
#     Host = localhost
#     Port = 7878
#   }
# }
# """).lstrip("\n")
_config_template=dedent("""
Devices = <<<devices_file_path>>>
Port = <<<port>>>
EnableSourceDeviceModels = true

Adapters {
<<<adapters>>>
}
""").lstrip("\n")

# _devices_template=dedent("""
# <?xml version="1.0" encoding="utf-8"?>
# <MTConnectDevices
# xmlns="urn:mtconnect.org:MTConnectDevices:2.7"
# xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
# xsi:schemaLocation="urn:mtconnect.org:MTConnectDevices:2.7 ./schemas/MTConnectDevices_2.7.xsd">
# <Header creationTime="2026-05-01T00:00:00Z" sender="localhost"
#     assetBufferSize="1024" assetCount="0" version="2.7" instanceId="1"
#     bufferSize="131072"/>
# <Devices />
# </MTConnectDevices>
# """).lstrip("\n")
_devices_template=dedent("""
<?xml version="1.0" encoding="utf-8"?>
<MTConnectDevices
xmlns="urn:mtconnect.org:MTConnectDevices:<<<mtconnect_version>>>"
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
xsi:schemaLocation="urn:mtconnect.org:MTConnectDevices:<<<mtconnect_version>>> ./schemas/MTConnectDevices_<<<mtconnect_version>>>.xsd">
<Header creationTime="<<<creation_time>>>" sender="localhost"
    assetBufferSize="<<<asset_buffer_size>>>" assetCount="0" version="<<<mtconnect_version>>>" instanceId="1"
    bufferSize="<<<buffer_size>>>"/>
<Devices />
</MTConnectDevices>
""").lstrip("\n")

class AgentConfig(BaseModel):
    adapters: List[AgentAdapter] = Field(
        description="The adapters the agent should collect data from",
    )
    devices_file_path: str = Field(
        "Devices.xml",
        description="The path to the devices file",
    )
    port: int = Field(
        5000,
        description="The port the agent should run on",
    )
    mtconnect_version: str = Field(
        "2.7",
        description="The MTConnect version to use",
    )
    asset_buffer_size: int = Field(
        1024,
    )
    buffer_size: int = Field(
        131072,
    )
    creation_time: Annotated[
        datetime,
        PlainSerializer(lambda dt: dt.isoformat(), return_type=str)
    ] = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )

    def get_config(self) -> str:
        adapters = ""
        for a in self.adapters:
            adapters += a.get_adapter_config()
        return _config_template \
            .replace("<<<devices_file_path>>>", self.devices_file_path) \
            .replace("<<<port>>>", str(self.port)) \
            .replace("<<<adapters>>>", indent(adapters.strip("\n"), "  "))
    
    def get_devices(self) -> str:
        return _devices_template \
            .replace("<<<mtconnect_version>>>", self.mtconnect_version) \
            .replace("<<<asset_buffer_size>>>", str(self.asset_buffer_size)) \
            .replace("<<<buffer_size>>>", str(self.buffer_size)) \
            .replace("<<<creation_time>>>", self.creation_time.isoformat())


class Agent:

    def _find_and_terminate_by_port(self, port: int, host: Optional[str] = None) -> None:
        # Only works on windows because it's a separate process
        # host = "0.0.0.0"
        if not isinstance(port, int):
            raise TypeError("port must be an int")
        if host is not None:
            if not isinstance(host, str):
                raise TypeError("host must be an Optional[str]")
        
        if host is not None:
            for conn in psutil.net_connections():
                if conn.laddr.port == port and conn.laddr.ip == host:
                    if conn.pid == 0:
                        return # This is a special case where the process is not running
                    try:
                        proc = psutil.Process(conn.pid)
                        proc.terminate()
                        proc.wait(timeout=5)
                    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                        proc.kill()
        else:
            for conn in psutil.net_connections():
                if conn.laddr.port == port:
                    if conn.pid == 0:
                        return # This is a special case where the process is not running
                    try:
                        psutil.Process(conn.pid).terminate()
                    except psutil.NoSuchProcess:
                        pass

    def __init__(self, config: AgentConfig):
        if not isinstance(config, AgentConfig):
            raise TypeError("config must be an AgentConfig")
        self._config = config

        self._temp_dir: Optional[str] = None
        self._process: Optional[subprocess.Popen] = None
        self._process_pid: Optional[int] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

    @property
    def config(self) -> AgentConfig:
        return self._config
    
    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None
    
    def start(self) -> "Agent":
        if os.name == "nt":
            # On windows, kill any rogue agents running on the port that will be used.
            self._find_and_terminate_by_port(self._config.port)

        self._temp_dir = tempfile.mkdtemp(prefix="mtconnect-agent-", suffix="-tmp")
        self._config.devices_file_path = os.path.join(self._temp_dir, "Devices.xml")
        config_file_path = os.path.join(self._temp_dir, "agent.cfg")
        Path(self._config.devices_file_path).write_text(self._config.get_devices())
        Path(config_file_path).write_text(self._config.get_config())

        bin_path = os.path.join(Path(os.path.abspath(__file__)).parent, "bin")
        if os.name == 'nt':
            # Windows
            bin_path = os.path.join(bin_path, "agent.exe")
            args = [bin_path, "run"]
        else:
            # Linux
            bin_path = os.path.join(bin_path, "agent")
            #args = f'"{bin_path}" debug'
            args = [bin_path, "run"]

        # Start the agent
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._temp_dir,
            #preexec_fn=os.setsid,  # Detach from parent process group
            #start_new_session=True,  # Start a new session
        )
        self._process_pid = self._process.pid

        self._stdout_thread = threading.Thread(
            target=self._pipe_to_logger,
            args=(self._process.stdout, agent_logger.debug),
            name="mtconnect-agent-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._pipe_to_logger,
            args=(self._process.stderr, agent_logger.error),
            name="mtconnect-agent-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        if os.name != "nt":
            # Can delete immediately on linux. Can't on Windows because it has files locked.
            shutil.rmtree(self._temp_dir)
            self._temp_dir = None

        # Allow time for the agent to start and load files, or fail and exit.
        timeout_time = time.time() + 5.0
        while time.time() < timeout_time:
            if self._process.poll() is not None:
                raise Exception("agent failed to start")
            time.sleep(0.001)
        
        return self

    @staticmethod
    def _pipe_to_logger(stream: io.BufferedReader, log: Callable[[str], None]) -> None:
        try:
            for line in iter(stream.readline, b""):
                if not line:
                    break
                log(line.decode("utf-8", errors="replace").rstrip())
        except Exception as ex:
            logger.error(f"agent pipe reader exception: {ex}")

    def stop(self) -> None:
        if self._process is None:
            raise Exception("agent not started")
        timeout_time = time.time() + 5.0
        if self._process.poll() is None:
            # Process is still running

            # Try to terminate
            self._process.terminate()
            while self._process.poll() is None and time.time() < timeout_time:
                time.sleep(0.001)
                self._process.terminate()

            # Try to kill
            timeout_time = time.time() + 5.0
            while self._process.poll() is None and time.time() < timeout_time:
                time.sleep(0.001)
                self._process.kill()

            # Warn that couldn't stop
            if self._process.poll() is None:
                raise Exception("could not stop agent")
        
        # Windows doesn't kill agent sometimes
        if self._process_pid is not None:
            try:
                proc = psutil.Process(self._process_pid)
                proc.terminate()
                proc.wait(timeout=5)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                proc.kill()
            finally:
                self._process_pid = None

        # Wait for the stdout/stderr reader threads to drain and exit. Once the
        # process has been killed, its pipes close and readline returns b"".
        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None:
                t.join(timeout=5)
        self._stdout_thread = None
        self._stderr_thread = None

        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir)
