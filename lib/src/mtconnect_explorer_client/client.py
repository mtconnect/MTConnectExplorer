"""Sync httpx client for the MTConnectExplorer API (docs/api.md).

Flat, reference-style methods — one per endpoint, named ``<service>_<verb>``.
Create methods take the endpoint's pydantic model (capture/play also accept
its fields as keyword arguments)::

    from mtconnect_explorer_client import ApiClient, Mill3AxisCreate, LinearAxisLimits

    client = ApiClient("http://127.0.0.1:8501/api/v1", api_key="...")
    sid = client.simulate_start(Mill3AxisCreate(x=LinearAxisLimits(max=0.5))).id
    for event in client.events_stream(scope="simulate"):
        print(event)
"""

from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Union

import httpx

from .events import AnyEvent, iter_events
from .exceptions import ApiError
from .schemas import (
    AgentInfo,
    CaptureCreate,
    CaptureSessionInfo,
    DatasetDevices,
    DatasetInfo,
    EquipmentInfo,
    EventPage,
    HealthInfo,
    PlayCreate,
    PlayProgress,
    PlaySeek,
    PlaySessionInfo,
    PortsInfo,
    RenameRequest,
    RoutineInfo,
    SimulateCreate,
    SimulateSessionInfo,
    VersionInfo,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8501/api/v1"


class ApiClient:
    """Synchronous client for a running MTConnectExplorer instance."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    # -- plumbing --------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise ApiError.from_response(response)
        return response

    # -- System ----------------------------------------------------------------
    def health(self) -> HealthInfo:
        """GET /health (unauthenticated liveness probe)."""
        return HealthInfo.model_validate(self._request("GET", "/health").json())

    def version(self) -> VersionInfo:
        """GET /version."""
        return VersionInfo.model_validate(self._request("GET", "/version").json())

    def agents_list(self) -> List[AgentInfo]:
        """GET /agents — local agents run by live Play/Simulate sessions."""
        return [AgentInfo.model_validate(a) for a in self._request("GET", "/agents").json()]

    def ports(self, shdr_count: int = 1) -> PortsInfo:
        """GET /ports — used ports plus free suggestions for launch forms."""
        return PortsInfo.model_validate(
            self._request("GET", "/ports", params={"shdr_count": shdr_count}).json()
        )

    # -- Datasets ----------------------------------------------------------------
    def datasets_list(self) -> List[DatasetInfo]:
        """GET /datasets."""
        return [DatasetInfo.model_validate(d) for d in self._request("GET", "/datasets").json()]

    def datasets_get(self, name: str) -> DatasetInfo:
        """GET /datasets/{name}."""
        return DatasetInfo.model_validate(self._request("GET", f"/datasets/{name}").json())

    def datasets_upload(self, name: str, file: Union[bytes, BinaryIO]) -> DatasetInfo:
        """POST /datasets/upload (multipart; overwriting is not allowed)."""
        response = self._request(
            "POST",
            "/datasets/upload",
            files={"file": (name, file, "application/octet-stream")},
            timeout=None,  # large uploads
        )
        return DatasetInfo.model_validate(response.json())

    def datasets_download(self, name: str) -> Iterator[bytes]:
        """GET /datasets/{name}/content — yields the file in chunks."""
        with self._client.stream(
            "GET", f"/datasets/{name}/content", timeout=None
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise ApiError.from_response(response)
            yield from response.iter_bytes(chunk_size=1024 * 1024)

    def datasets_rename(self, name: str, new_name: str) -> None:
        """PATCH /datasets/{name}."""
        self._request(
            "PATCH", f"/datasets/{name}",
            json=RenameRequest(new_name=new_name).model_dump(),
        )

    def datasets_delete(self, name: str) -> None:
        """DELETE /datasets/{name}."""
        self._request("DELETE", f"/datasets/{name}")

    def datasets_devices(self, name: str) -> DatasetDevices:
        """GET /datasets/{name}/devices — devices from the probe frame."""
        return DatasetDevices.model_validate(
            self._request("GET", f"/datasets/{name}/devices").json()
        )

    # -- Capture -----------------------------------------------------------------
    def capture_start(
        self, create: Optional[CaptureCreate] = None, **fields: Any
    ) -> CaptureSessionInfo:
        """POST /capture/sessions."""
        body = create if create is not None else CaptureCreate(**fields)
        return CaptureSessionInfo.model_validate(
            self._request("POST", "/capture/sessions", json=body.model_dump()).json()
        )

    def capture_list(self) -> List[CaptureSessionInfo]:
        """GET /capture/sessions."""
        return [
            CaptureSessionInfo.model_validate(s)
            for s in self._request("GET", "/capture/sessions").json()
        ]

    def capture_get(self, session_id: str) -> CaptureSessionInfo:
        """GET /capture/sessions/{id}."""
        return CaptureSessionInfo.model_validate(
            self._request("GET", f"/capture/sessions/{session_id}").json()
        )

    def capture_stop(self, session_id: str) -> None:
        """DELETE /capture/sessions/{id} — stop and remove."""
        self._request("DELETE", f"/capture/sessions/{session_id}")

    def capture_stop_all(self) -> None:
        """DELETE /capture/sessions — stop-all."""
        self._request("DELETE", "/capture/sessions")

    # -- Play ----------------------------------------------------------------------
    def play_start(
        self, create: Optional[PlayCreate] = None, **fields: Any
    ) -> PlaySessionInfo:
        """POST /play/sessions."""
        body = create if create is not None else PlayCreate(**fields)
        return PlaySessionInfo.model_validate(
            self._request("POST", "/play/sessions", json=body.model_dump()).json()
        )

    def play_list(self) -> List[PlaySessionInfo]:
        """GET /play/sessions."""
        return [
            PlaySessionInfo.model_validate(s)
            for s in self._request("GET", "/play/sessions").json()
        ]

    def play_get(self, session_id: str) -> PlaySessionInfo:
        """GET /play/sessions/{id}."""
        return PlaySessionInfo.model_validate(
            self._request("GET", f"/play/sessions/{session_id}").json()
        )

    def play_progress(self, session_id: str) -> PlayProgress:
        """GET /play/sessions/{id}/progress — the scrub bar's playhead origin."""
        return PlayProgress.model_validate(
            self._request("GET", f"/play/sessions/{session_id}/progress").json()
        )

    def play_seek(self, session_id: str, position: float) -> None:
        """POST /play/sessions/{id}/seek — seek a running playback (re-timestamp mode)."""
        self._request(
            "POST", f"/play/sessions/{session_id}/seek",
            json=PlaySeek(position=position).model_dump(),
        )

    def play_pause(self, session_id: str) -> None:
        """POST /play/sessions/{id}/pause — pause a running playback (any timestamp mode)."""
        self._request("POST", f"/play/sessions/{session_id}/pause")

    def play_resume(self, session_id: str) -> None:
        """POST /play/sessions/{id}/resume — resume a paused playback."""
        self._request("POST", f"/play/sessions/{session_id}/resume")

    def play_stop(self, session_id: str) -> None:
        """DELETE /play/sessions/{id} — stop and remove."""
        self._request("DELETE", f"/play/sessions/{session_id}")

    def play_stop_all(self) -> None:
        """DELETE /play/sessions — stop-all."""
        self._request("DELETE", "/play/sessions")

    # -- Simulate ---------------------------------------------------------------------
    def simulate_equipment(self) -> List[EquipmentInfo]:
        """GET /simulate/equipment — catalog for the launch form."""
        return [
            EquipmentInfo.model_validate(e)
            for e in self._request("GET", "/simulate/equipment").json()
        ]

    def simulate_start(self, create: SimulateCreate) -> SimulateSessionInfo:
        """POST /simulate/start/{equipment_type} — dispatched from the create
        model's type (Mill3AxisCreate, Mill5AxisTrunnionBcCreate, Robot6DofCreate)."""
        return SimulateSessionInfo.model_validate(
            self._request(
                "POST",
                f"/simulate/start/{create.equipment_type}",
                json=create.model_dump(),
            ).json()
        )

    def simulate_list(self) -> List[SimulateSessionInfo]:
        """GET /simulate/sessions."""
        return [
            SimulateSessionInfo.model_validate(s)
            for s in self._request("GET", "/simulate/sessions").json()
        ]

    def simulate_get(self, session_id: str) -> SimulateSessionInfo:
        """GET /simulate/sessions/{id}."""
        return SimulateSessionInfo.model_validate(
            self._request("GET", f"/simulate/sessions/{session_id}").json()
        )

    def simulate_routine(self, session_id: str) -> RoutineInfo:
        """GET /simulate/sessions/{id}/routine — feeds the phase-locked 3D view."""
        return RoutineInfo.model_validate(
            self._request("GET", f"/simulate/sessions/{session_id}/routine").json()
        )

    def simulate_stop(self, session_id: str) -> None:
        """DELETE /simulate/sessions/{id} — stop and remove."""
        self._request("DELETE", f"/simulate/sessions/{session_id}")

    def simulate_stop_all(self) -> None:
        """DELETE /simulate/sessions — stop-all."""
        self._request("DELETE", "/simulate/sessions")

    # -- Events -----------------------------------------------------------------------
    def events(
        self,
        after: Optional[int] = None,
        scope: Optional[str] = None,
        session: Optional[str] = None,
        level: Optional[str] = None,
        limit: int = 500,
    ) -> EventPage:
        """GET /events — poll mode with an exclusive ``after`` cursor."""
        params: Dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        if scope is not None:
            params["scope"] = scope
        if session is not None:
            params["session"] = session
        if level is not None:
            params["level"] = level
        return EventPage.model_validate(
            self._request("GET", "/events", params=params).json()
        )

    def events_stream(
        self,
        after: Optional[int] = None,
        scope: Optional[str] = None,
        session: Optional[str] = None,
        level: Optional[str] = None,
        reconnect: bool = True,
    ) -> Iterator[AnyEvent]:
        """GET /events/stream — NDJSON iterator with seq-based reconnect.

        Yields every event including pings (their ``seq`` is a cursor
        checkpoint). Iterate forever, or pass ``reconnect=False`` to stop when
        the connection drops."""
        return iter_events(
            self._client,
            after=after,
            scope=scope,
            session=session,
            level=level,
            reconnect=reconnect,
        )
