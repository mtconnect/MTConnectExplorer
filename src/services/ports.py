"""Port bookkeeping (docs/api.md §4).

The used-set is *derived from the live session table* — there is no separate
reservation store to leak. A session holds its ports through every
non-terminal state including ``stopping``; the moment it reaches
``finished``/``error`` or is deleted, its ports are free. Atomicity comes from
validating inside the manager's start() while holding sessions.STATE_LOCK.
"""

from typing import Any, Dict, List, Optional

from mtconnect_explorer_client.schemas import AgentInfo, PortsInfo, PortSuggestions

from .errors import Conflict
from . import sessions

# Form-seed defaults (mirror AgentConfig.port / AgentAdapter.port).
DEFAULT_AGENT_PORT = 5000
DEFAULT_SHDR_PORT = 7878


def agent_url(port: int) -> str:
    """Base URL of a locally-running MTConnect agent on the given port."""
    return f"http://localhost:{port}/"


def port_holders() -> Dict[int, str]:
    """Map of port -> holding session id across every live (non-terminal) session."""
    holders: Dict[int, str] = {}
    for sess in sessions.all_live_sessions():
        if sess.ports.agent is not None:
            holders.setdefault(sess.ports.agent, sess.id)
        for p in sess.ports.shdr:
            holders.setdefault(p, sess.id)
    return holders


def used_ports() -> set:
    """Agent + SHDR ports in use by every live Play and Simulate session."""
    return set(port_holders())


def next_free_port(start: int, used: set) -> int:
    p = start
    while p in used:
        p += 1
    return p


def suggest_ports(shdr_count: int = 1) -> PortsInfo:
    """Used ports plus free suggestions for pre-filling launch forms."""
    used = used_ports()
    agent = next_free_port(DEFAULT_AGENT_PORT, used)
    taken = set(used) | {agent}
    shdr: List[int] = []
    p = DEFAULT_SHDR_PORT
    for _ in range(max(0, shdr_count)):
        p = next_free_port(p, taken)
        taken.add(p)
        shdr.append(p)
        p += 1
    return PortsInfo(
        used=sorted(used),
        suggested=PortSuggestions(agent=agent, shdr=shdr),
    )


def validate_ports(agent_port: Optional[int], shdr_ports: List[int]) -> None:
    """All-or-nothing port validation for a session create. Caller holds
    sessions.STATE_LOCK so validate-and-reserve is atomic.

    Raises Conflict(code=port_conflict) listing every conflict: ports held by
    live sessions, SHDR ports equal to the agent port, and SHDR ports reused
    within the request."""
    holders = port_holders()
    conflicts: List[Dict[str, Any]] = []
    messages: List[str] = []

    if agent_port is not None and agent_port in holders:
        conflicts.append({"port": agent_port, "held_by": holders[agent_port], "kind": "agent"})
        messages.append(f"agent port {agent_port} is in use by {holders[agent_port]}")

    seen: Dict[int, int] = {}
    for p in shdr_ports:
        if p in holders:
            conflicts.append({"port": p, "held_by": holders[p], "kind": "shdr"})
            messages.append(f"SHDR port {p} is in use by {holders[p]}")
        if agent_port is not None and p == agent_port:
            conflicts.append({"port": p, "held_by": "request", "kind": "shdr"})
            messages.append(f"SHDR port {p} equals the agent port")
        seen[p] = seen.get(p, 0) + 1
    for p, n in seen.items():
        if n > 1:
            conflicts.append({"port": p, "held_by": "request", "kind": "shdr"})
            messages.append(f"SHDR port {p} reused {n} times in the request")

    if conflicts:
        raise Conflict(
            "; ".join(messages),
            code="port_conflict",
            context={"conflicts": conflicts},
        )


def running_agents() -> List[AgentInfo]:
    """Base URLs of local agents run by live Play/Simulate sessions, de-duplicated,
    with the session ids running each."""
    by_url: Dict[str, List[str]] = {}
    for sess in sessions.all_live_sessions():
        if sess.ports.agent is not None:
            by_url.setdefault(agent_url(sess.ports.agent), []).append(sess.id)
    return [AgentInfo(base_url=u, sessions=ids) for u, ids in by_url.items()]


def running_agent_urls() -> List[str]:
    """Base URLs only (feeds the Explore/Capture quick-fill buttons)."""
    return [a.base_url for a in running_agents()]
