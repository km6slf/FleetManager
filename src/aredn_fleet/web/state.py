"""Application state and background jobs for the GUI.

Jobs run as asyncio tasks inside the server's event loop. The browser polls
``/api/jobs/<id>?since=N`` for new log lines. Only one job that changes nodes
(upgrade, radio) may run at a time; read-only jobs (discover, firmware fetch,
dry runs) are not limited. "Stop" is cooperative: a firmware rollout stops
before the next wave and never interrupts a node mid-flash.
"""

from __future__ import annotations

from ..routing import routing_of

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
    fingerprint: str = ""      # what a dry run reviewed; an execute must match it
    dry_run: bool = False

    def log(self, msg: str, level: str = "info", **data: Any) -> None:
        self.events.append({"t": time.time(), "level": level, "msg": msg, **data})

    def text(self) -> str:
        """The job log as plain text, as shown in the Activity tab, plus error traces."""
        lines = [f"{self.title} — {self.status}",
                 f"started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}", ""]
        for ev in self.events:
            ts = time.strftime("%H:%M:%S", time.localtime(ev["t"]))
            lines.append(f"{ts}  [{ev['level']}] {ev['msg']}")
            if ev.get("trace"):
                lines.append(ev["trace"].rstrip())
        return "\n".join(lines) + "\n"

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                "dry_run": self.dry_run,
                "mutating": self.mutating, "started": self.started, "finished": self.finished,
                "events": len(self.events), "stop_requested": self.stop_requested}


class JobBusy(Exception):
    pass


class JobManager:
    def __init__(self, log_dir: Path | None = None) -> None:
        self.jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self.log_dir = log_dir          # finished jobs' logs are saved here

    def log_path(self, job: Job) -> Path | None:
        if not self.log_dir:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(job.started))
        return self.log_dir / f"{stamp}-{job.kind}-{job.id}.txt"

    def save_log(self, job: Job) -> Path | None:
        path = self.log_path(job)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(job.text(), encoding="utf-8")
        return path

    def active_mutating(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.mutating and j.status == "running"), None)

    def reviewed(self, job_id: str | None, kind: str, fingerprint: str,
                 max_age: float = 900) -> str | None:
        """Why an execute may NOT proceed on the strength of dry run ``job_id`` (None = OK)."""
        j = self.jobs.get(job_id or "")
        if not j or j.kind != kind or not j.dry_run:
            return "review the plan first (run the dry run from the Upgrade… / Switch… button)"
        if j.status != "done":
            return "the reviewed dry run didn't finish cleanly; run it again"
        if j.fingerprint != fingerprint:
            return ("something changed since you reviewed the plan (nodes, settings or a node's own "
                    "settings); review it again")
        if time.time() - (j.finished or 0) > max_age:
            return "the review is more than 15 minutes old; review the plan again"
        if (j.result or {}).get("blocked"):
            return "the reviewed plan has blocked nodes; fix them first"
        return None

    def start(self, kind: str, title: str, mutating: bool,
              body: Callable[[Job], Awaitable[Any]], fingerprint: str = "",
              dry_run: bool = False) -> Job:
        if mutating and (busy := self.active_mutating()):
            raise JobBusy(f"'{busy.title}' is still running; wait for it to finish")
        job = Job(uuid.uuid4().hex[:10], kind, title, mutating, fingerprint=fingerprint,
                  dry_run=dry_run)
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
                try:
                    self.save_log(job)
                except OSError:
                    pass

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
        self.no_babel: set[str] = set()   # tiny-build hardware: OLSR only on every release
        self.known_hosts = self.config_dir / "known_hosts"
        self.overrides_path = self.config_dir / "overrides.json"
        self.mirror = "https://downloads.arednmesh.org/"
        self.jobs = JobManager(self.config_dir / "logs" / "jobs")
        self.topology = Topology()
        self.nodes: dict[str, dict[str, Any]] = {}
        self.root = ""
        self.discovered_at: float | None = None
        self.errors: dict[str, str] = {}
        self.radios: dict[str, dict[str, Any]] = {}   # node -> NodeRadios.to_json()
        self.connector = None   # tests inject a fake; None means real SSH
        self.sysinfo = sysinfo_or_none
        self.settings: dict[str, Any] = {}
        self.load_topology()
        self.overrides: dict[str, dict[str, Any]] = self._load_overrides()

    # ---------------------------------------------------------------- node-specific settings
    def _load_overrides(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.overrides_path.read_text())
        except (OSError, ValueError):
            return {}

    def set_override(self, node: str, radio: dict[str, dict[str, Any]], note: str = "") -> None:
        clean = {b: {k: v for k, v in vals.items() if v not in (None, "")}
                 for b, vals in radio.items()}
        clean = {b: v for b, v in clean.items() if v}
        if clean:
            self.overrides[node] = {"radio": clean, "note": note, "updated": time.time()}
        else:
            self.overrides.pop(node, None)
        self._save_overrides()

    def delete_override(self, node: str) -> None:
        self.overrides.pop(node, None)
        self._save_overrides()

    def _save_overrides(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.overrides_path.write_text(json.dumps(self.overrides, indent=1))

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
        self.radios = data.get("radios", {})
        self.settings = data.get("settings", {})

    def set_discovery(self, topo: Topology, infos: dict[str, NodeInfo], errors: dict[str, str],
                      root: str) -> None:
        self.topology = topo
        self.nodes = {n: {"firmware": i.firmware, "model": i.model, "board_id": i.board_id,
                          "api_version": i.api_version, "lat": i.lat, "lon": i.lon,
                          "meshrf": (i.raw.get("meshrf") or {}),
                          # True when this node shares its WAN with the mesh (sysinfo reports it
                          # on 4.26.x and the nightly; absent on older firmware).
                          "gateway": (i.raw.get("node_details") or {}).get("mesh_gateway")}
                      for n, i in infos.items()}
        self.errors = errors
        self.root = root
        self.discovered_at = time.time()
        self.radios = {n: r for n, r in self.radios.items() if n in topo.nodes}
        self.save_topology()

    def save_topology(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.topo_path.write_text(json.dumps({
            "nodes": sorted(self.topology.nodes), "links": [l.__dict__ for l in self.topology.links],
            "info": self.nodes, "errors": self.errors, "root": self.root,
            "discovered_at": self.discovered_at, "radios": self.radios,
            "settings": self.settings}, indent=1))

    def radios_stale(self, node: str) -> bool:
        """Radio details read on different firmware than the node runs now."""
        inv, fw = self.radios.get(node) or {}, (self.nodes.get(node) or {}).get("firmware", "")
        return bool(inv.get("firmware") and fw and inv["firmware"] != fw)

    def set_firmware(self, node: str, version: str) -> None:
        self.nodes.setdefault(node, {})["firmware"] = version
        self.save_topology()

    def set_settings(self, node: str, ns) -> None:
        """Settings read from a node (its own dialogs' switches and their current values)."""
        self.settings[node] = ns.to_json()
        self.save_topology()

    def settings_summary(self, nodes: list[str]) -> dict[str, Any]:
        from ..jobs.settings import NodeSettings, summarize
        have = {n: NodeSettings.from_json(self.settings[n]) for n in nodes if n in self.settings}
        fw = {n: (self.nodes.get(n) or {}).get("firmware", "") for n in have}
        stale = sorted(n for n, ns in have.items() if fw[n] and ns.firmware and fw[n] != ns.firmware)
        for n in stale:
            have.pop(n)
        return {"rows": summarize(have), "missing": sorted(set(nodes) - set(have) - set(stale)),
                "stale": stale, "read": sorted(have)}

    def set_radios(self, node: str, inv) -> None:
        self.radios[node] = inv.to_json()
        self.save_topology()

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
                "radios": radio_summary(self.radios.get(n)),
                "radios_stale": self.radios_stale(n),
                "custom": custom_summary(self.overrides.get(n)),
                "radios_read_at": (self.radios.get(n) or {}).get("read_at"),
                "gateway": info.get("gateway"),
                "routing": routing_of(info.get("firmware", ""), n not in self.no_babel),
            })
        return {"root": self.root, "discovered_at": self.discovered_at, "nodes": nodes,
                "links": [l.__dict__ for l in self.topology.links]}


async def sysinfo_or_none(node: str) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(http_fetch, node, 8.0)
    except Exception:  # noqa: BLE001
        return None


def radio_summary(inv_json: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Compact per-radio view for the node table (no channel tables)."""
    if not inv_json:
        return None
    from ..jobs.radio import MODE_LABELS, NodeRadios, band_label
    inv = NodeRadios.from_json(inv_json)
    return [{"band": r.band, "band_label": band_label(r.band), "mode": r.mode, "mode_label": MODE_LABELS.get(r.mode, r.mode),
             "channel": r.channel, "bandwidth": r.bandwidth,
             "label": r.label(r.channel, r.bandwidth)}
            for r in sorted(inv.radios, key=_radio_freq)]


def _radio_freq(r) -> int:
    fs = [c["frequency"] for lst in r.channels.values() for c in lst if c.get("frequency")]
    return min(fs) if fs else 0


def radio_options(radios: dict[str, dict[str, Any]], nodes: list[str],
                  stale: set[str] | None = None) -> dict[str, Any]:
    """Choices for the bulk radio panel: per band, what every selected node can do.

    Widths and channels are the intersection across the selected nodes that
    have that band, so any choice offered is valid on all of them. Channel
    options are built like the node's own dialog: ``channels[width]`` with the
    node's labels, minus ``managedOOB`` in PtMP/PtP/Station modes.
    """
    from ..jobs.radio import MESH_MODES, NodeRadios, band_label

    stale = stale or set()
    known = {n: NodeRadios.from_json(radios[n]) for n in nodes if n in radios and n not in stale}
    missing = sorted(set(nodes) - set(known))
    bands: dict[str, dict[str, Any]] = {}
    for n, inv in known.items():
        for r in inv.radios:
            b = bands.setdefault(r.band, {"band": r.band, "nodes": [], "radios": []})
            b["nodes"].append(n)
            b["radios"].append(r)
    out = []
    def freq_order(item):
        rs = item[1]["radios"]
        fs = [c["frequency"] for r in rs for lst in r.channels.values() for c in lst if c.get("frequency")]
        return min(fs) if fs else 0

    for band, b in sorted(bands.items(), key=freq_order):
        rs = b["radios"]
        bws = sorted(set.intersection(*(set(r.bws) for r in rs)))
        options: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for cls, mode in (("mesh", "mesh"), ("managed", "meshsta")):
            per_bw = {}
            for bw in bws:
                common = None
                for r in rs:
                    nums = {int(c["number"]) for c in r.channel_options(mode, bw)}
                    common = nums if common is None else common & nums
                per_bw[str(bw)] = [c for c in rs[0].channel_options(mode, bw)
                                   if int(c["number"]) in (common or set())]
            options[cls] = per_bw
        modes = {r.mode for r in rs}
        # Purposes every selected radio on this band supports (None where a node didn't say).
        sup = [set(r.modes) for r in rs if r.modes is not None]
        supported = sorted(set.intersection(*sup)) if sup else None
        chans = {(r.channel, r.bandwidth) for r in rs if r.mode in MESH_MODES}
        out.append({
            "band": band, "band_label": band_label(band), "nodes": sorted(b["nodes"]),
            "per_node": {n: next(({"mode": r.mode, "channel": r.channel, "bandwidth": r.bandwidth,
                                   "label": r.label(r.channel, r.bandwidth), "ssid": r.ssid}
                                  for r in known[n].radios if r.band == band), None)
                         for n in sorted(b["nodes"])},
            "without": sorted(set(known) - set(b["nodes"])),
            "modes": sorted(modes), "supported_modes": supported, "bws": bws, "channels": options,
            "current": ({"channel": next(iter(chans))[0], "width": next(iter(chans))[1]}
                        if len(chans) == 1 else None),
            "current_mode": next(iter(modes)) if len(modes) == 1 else None,
            "current_ssid": (next(iter(ss)) if len(ss := {r.ssid for r in rs if r.mode in MESH_MODES})
                             == 1 else None),
        })
    multi = [inv.multi_mesh for inv in known.values()]
    return {"bands": out, "missing": missing, "stale": sorted(set(nodes) & stale),
            "multi_mesh": all(m is True for m in multi) if multi else False}


def custom_summary(ov: dict[str, Any] | None) -> dict[str, Any] | None:
    """Short description of a node's own settings, for the list marker and tooltips."""
    if not ov:
        return None
    from ..jobs.radio import MODE_LABELS, band_label
    parts = []
    for band, v in ov.get("radio", {}).items():
        bits = []
        if v.get("mode"):
            bits.append(MODE_LABELS.get(v["mode"], v["mode"]))
        if v.get("channel") is not None:
            bits.append(f"channel {v['channel']}")
        if v.get("width"):
            bits.append(f"{v['width']} MHz")
        if v.get("ssid"):
            bits.append(f"SSID {v['ssid']}")
        if v.get("peer"):
            bits.append(f"peer {v['peer']}")
        parts.append(f"{band_label(band)}: {', '.join(bits)}")
    return {"text": "; ".join(parts), "note": ov.get("note", ""), "radio": ov.get("radio", {}),
            "updated": ov.get("updated")}
