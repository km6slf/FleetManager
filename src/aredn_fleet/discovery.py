"""Discover the mesh by crawling each node's sysinfo.json.

``sysinfo.json?link_info=1`` is unauthenticated and present on every AREDN
release the manager cares about, so discovery needs no credentials. Each node
reports its direct neighbours under ``link_info`` (keyed by neighbour IP, with
``hostname`` and ``linkType``). Walking those breadth-first from the starting
node gives the whole reachable graph.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .topology import Topology

MESH_SUFFIX = ".local.mesh"


def normalize(hostname: str) -> str:
    h = hostname.strip().lower()
    if h.endswith(MESH_SUFFIX):
        h = h[: -len(MESH_SUFFIX)]
    return h


@dataclass
class NodeInfo:
    name: str
    firmware: str = ""
    model: str = ""
    board_id: str = ""
    api_version: str = ""
    lat: str = ""
    lon: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


Fetcher = Callable[[str], dict[str, Any]]


SYSINFO_PATHS = (
    ":80/a/sysinfo?link_info=1",              # AREDN 4.x UI (cgi path 307-redirects here)
    ":80/cgi-bin/sysinfo.json?link_info=1",   # older firmware (api_version 1.x, Lua UI)
    ":8080/cgi-bin/sysinfo.json?link_info=1", # oldest firmware served its pages on 8080 only
)


def http_fetch(node: str, timeout: float = 8.0) -> dict[str, Any]:
    host = node if "." in node else node + MESH_SUFFIX
    last: Exception | None = None
    for path in SYSINFO_PATHS:
        try:
            with urllib.request.urlopen(f"http://{host}{path}", timeout=timeout) as resp:  # noqa: S310
                return json.load(resp)
        except urllib.error.HTTPError as exc:  # 404 on firmware without this path
            last = exc
        except urllib.error.URLError as exc:  # port 8080 closed on newer firmware
            last = exc
            if path.startswith(":80/"):
                raise
    raise last or OSError("no sysinfo")


def link_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """``link_info`` is a map keyed by neighbour IP; old firmware sends ``[]`` when empty."""
    li = data.get("link_info") or {}
    if isinstance(li, dict):
        return [e for e in li.values() if isinstance(e, dict)]
    if isinstance(li, list):
        return [e for e in li if isinstance(e, dict)]
    return []


def parse_node(data: dict[str, Any]) -> tuple[NodeInfo, list[tuple[str, str]]]:
    node = data.get("node_details", {}) or {}
    info = NodeInfo(
        name=normalize(str(data.get("node", ""))),
        firmware=str(node.get("firmware_version", "")),
        model=str(node.get("model", "")),
        board_id=str(node.get("board_id", "")),
        api_version=str(data.get("api_version", "")),
        lat=str(data.get("lat", "")),
        lon=str(data.get("lon", "")),
        raw=data,
    )
    neighbours: list[tuple[str, str]] = []
    for entry in link_entries(data):
        host = entry.get("hostname")
        if host:
            neighbours.append((normalize(host), str(entry.get("linkType") or "RF").upper()))
    return info, neighbours


@dataclass
class DiscoveryResult:
    topology: Topology
    nodes: dict[str, NodeInfo]
    errors: dict[str, str]
    start: str = ""  # real name of the node discovery started from (the manager's root)


def crawl(start: str = "localnode", fetch: Fetcher = http_fetch, workers: int = 8,
          max_nodes: int = 1000) -> DiscoveryResult:
    topo = Topology()
    nodes: dict[str, NodeInfo] = {}
    errors: dict[str, str] = {}

    def one(name: str):
        try:
            return name, parse_node(fetch(name)), None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return name, None, str(exc)

    # The start alias (e.g. "localnode") resolves to a real node name.
    _, parsed, err = one(start)
    if parsed is None:
        raise ConnectionError(f"cannot reach {start}: {err}")
    first, _ = parsed
    seen = {start, first.name}
    frontier = [first.name]
    results = {first.name: parsed}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while frontier and len(nodes) < max_nodes:
            next_frontier: list[str] = []
            to_fetch = [n for n in frontier if n not in results]
            for name, parsed, err in pool.map(one, to_fetch):
                if parsed is None:
                    errors[name] = err or "unknown error"
                    topo.nodes.add(name)
                else:
                    results[name] = parsed
            for name in frontier:
                if name not in results:
                    continue
                info, neigh = results[name]
                nodes[name] = info
                topo.nodes.add(name)
                for other, kind in neigh:
                    topo.add_link(name, other, kind)
                    if other not in seen:
                        seen.add(other)
                        next_frontier.append(other)
            frontier = next_frontier

    # Each link is reported from both ends; keep one copy per (pair, kind).
    uniq = {}
    for link in topo.links:
        uniq.setdefault((frozenset((link.a, link.b)), link.kind), link)
    topo.links = list(uniq.values())
    return DiscoveryResult(topo, nodes, errors, first.name)
