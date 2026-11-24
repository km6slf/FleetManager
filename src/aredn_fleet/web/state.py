"""Application state and background jobs for the GUI.

Jobs run as asyncio tasks inside the server's event loop. The browser polls
``/api/jobs/<id>?since=N`` for new log lines. Only one job that changes nodes
(upgrade, radio) may run at a time; read-only jobs (discover, firmware fetch,
dry runs) are not limited. "Stop" is cooperative: a firmware rollout stops
before the next wave and never interrupts a node mid-flash.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import paths
from ..credentials import CredentialStore
from ..discovery import NodeInfo, http_fetch
from ..topology import Topology, dependents, hop_distances


@dataclass
class Job:
    id: str
    kind: str
    title: str
    mutating: bool
    status: str = "running"          # running, done, failed, stopped
    started: float = field(default_factory=time.time)
    finished: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    result: Any = None
    stop_requested: bool = False

    def log(self, msg: str, level: str = "info", **data: Any) -> None:
        self.events.append({"t": time.time(), "level": level, "msg": msg, **data})

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                "mutating": self.mutating, "started": self.started, "finished": self.finished,
                "events": len(self.events), "stop_requested": self.stop_requested}


class JobBusy(Exception):
    pass


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def active_mutating(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.mutating and j.status == "running"), None)

    def start(self, kind: str, title: str, mutating: bool,
              body: Callable[[Job], Awaitable[Any]]) -> Job:
        if mutating and (busy := self.active_mutating()):
            raise JobBusy(f"'{busy.title}' is still running; wait for it to finish")
        job = Job(uuid.uuid4().hex[:10], kind, title, mutating)
        self.jobs[job.id] = job

        async def runner():
            try:
                job.result = await body(job)
                if job.status == "running":
                    job.status = "stopped" if job.stop_requested else "done"
            except Exception as exc:  # noqa: BLE001 - surface everything in the UI
                job.status = "failed"
                job.log(f"{type(exc).__name__}: {exc}", "error", trace=traceback.format_exc())
            finally:
                job.finished = time.time()

        self._tasks[job.id] = asyncio.get_running_loop().create_task(runner())
        return job

    def list(self) -> list[dict[str, Any]]:
        return [j.summary() for j in sorted(self.jobs.values(), key=lambda j: -j.started)]


class AppState:
    """Everything the GUI needs, loaded from the same files the CLI uses."""

    def __init__(self, fw_dir: Path | None = None, config_dir: Path | None = None):
        self.config_dir = config_dir or paths.config_dir()
        self.fw_dir = fw_dir or paths.firmware_dir()
        self.creds_path = self.config_dir / "credentials.json"
        self.topo_path = self.config_dir / "mesh.json"
        self.audit_path = self.config_dir / "audit.jsonl"
        self.known_hosts = self.config_dir / "known_hosts"
        self.mirror = "https://downloads.arednmesh.org/"
        self.jobs = JobManager()
        self.topology = Topology()
        self.nodes: dict[str, dict[str, Any]] = {}
        self.root = ""
        self.discovered_at: float | None = None
        self.errors: dict[str, str] = {}
        self.connector = None   # tests inject a fake; None means real SSH
        self.sysinfo = sysinfo_or_none
        self.load_topology()

    def connect_factory(self, creds: CredentialStore):
        if self.connector:
            return self.connector
        from ..transport import connect
        return lambda n: connect(n, creds, known_hosts=self.known_hosts)

    # ---------------------------------------------------------------- persistence
    def creds(self) -> CredentialStore:
        return CredentialStore.load(self.creds_path)

    def save_creds(self, store: CredentialStore) -> None:
        store.save(self.creds_path)

    def load_topology(self) -> None:
        if not self.topo_path.exists():
            return
        try:
            data = json.loads(self.topo_path.read_text())
        except ValueError:
            return
        topo = Topology()
        for n in data.get("nodes", []):
            topo.nodes.add(n)
        for l in data.get("links", []):
            topo.add_link(l["a"], l["b"], l.get("kind", "RF"))
        self.topology = topo
        self.nodes = data.get("info", {})
        self.root = data.get("root", "")
        self.discovered_at = data.get("discovered_at")
        self.errors = data.get("errors", {})

    def set_discovery(self, topo: Topology, infos: dict[str, NodeInfo], errors: dict[str, str],
                      root: str) -> None:
        self.topology = topo
        self.nodes = {n: {"firmware": i.firmware, "model": i.model, "board_id": i.board_id,
                          "api_version": i.api_version, "lat": i.lat, "lon": i.lon,
                          "meshrf": (i.raw.get("meshrf") or {})}
                      for n, i in infos.items()}
        self.errors = errors
        self.root = root
        self.discovered_at = time.time()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.topo_path.write_text(json.dumps({
            "nodes": sorted(topo.nodes), "links": [l.__dict__ for l in topo.links],
            "info": self.nodes, "errors": errors, "root": root,
            "discovered_at": self.discovered_at}, indent=1))

    # ---------------------------------------------------------------- views
    def topology_view(self) -> dict[str, Any]:
        adj = self.topology.adjacency()
        dist = hop_distances(adj, self.root) if self.root in adj else {}
        deps = dependents(adj, self.root) if self.root in adj else {}
        nodes = []
        for n in sorted(self.topology.nodes):
            info = self.nodes.get(n, {})
            rf = info.get("meshrf") or {}
            nodes.append({
                "name": n, "firmware": info.get("firmware", ""), "model": info.get("model", ""),
                "hop": dist.get(n), "cuts_off": sorted(deps.get(n, ())) if n != self.root else [],
                "reachable": n in dist, "error": self.errors.get(n, ""),
                "channel": rf.get("channel"), "chanbw": rf.get("chanbw"), "rfmode": rf.get("mode"),
                "neighbours": sorted(adj.get(n, ())),
            })
        return {"root": self.root, "discovered_at": self.discovered_at, "nodes": nodes,
                "links": [l.__dict__ for l in self.topology.links]}


async def sysinfo_or_none(node: str) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(http_fetch, node, 8.0)
    except Exception:  # noqa: BLE001
        return None
