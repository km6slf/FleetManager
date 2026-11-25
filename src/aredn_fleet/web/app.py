"""HTTP API and static UI for the Fleet Manager GUI.

Every ``/api`` call needs the session token: the desktop launcher opens
``/?token=...``, which sets an HttpOnly cookie. In headless mode
(``aredn-fleet serve``) the token is printed with the URL. This matters
because the API can reflash radios: binding to a LAN address without a token
would let anyone on that network do it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, paths
from ..credentials import Credential, forget_password, store_password
from ..discovery import crawl
from ..firmware import Catalog, Channel, FirmwareStore, ReleaseIndex, match_profile, select_channels
from ..topology import PlanError, plan_rollout
from ..transport import errtext
from .state import AppState, Job, JobBusy, radio_options

STATIC = Path(__file__).parent / "static"
COOKIE = "fm_token"


# ---------------------------------------------------------------- request bodies

class DiscoverReq(BaseModel):
    start: str = "localnode"


class CredReq(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    kind: str  # "key" | "password"
    key_path: str | None = None
    password: str | None = None


class FetchReq(BaseModel):
    scope: str = "mesh"            # mesh | all | board
    boards: list[str] = []
    stable: int = 2
    nightly: bool = True
    include_factory: bool = False
    version: str | None = None     # one specific release (e.g. 3.25.5.1), instead of the channels


class InstallReq(BaseModel):
    version: str
    profile: str


class OpenReq(BaseModel):
    path: str


class PlanReq(BaseModel):
    nodes: list[str]
    parallel: int = 1
    exclude_tunnels: bool = False


class UpgradeReq(PlanReq):
    version: str
    soak: float = 180
    execute: bool = False
    confirm: str = ""
    reviewed_job: str | None = None


class BandChangeReq(BaseModel):
    band: str
    mode: str | None = None
    channel: int | None = None
    width: int | None = None
    peer: str | None = None
    ssid: str | None = None


class RadioReq(BaseModel):
    nodes: list[str]
    changes: list[BandChangeReq] = []
    revert_after: int = 600
    execute: bool = False
    confirm: str = ""
    reviewed_job: str | None = None


class NodesReq(BaseModel):
    nodes: list[str]


class SettingChangeReq(BaseModel):
    key: str
    on: bool


class SettingsReq(BaseModel):
    nodes: list[str]
    changes: list[SettingChangeReq]
    execute: bool = False
    confirm: str = ""
    reviewed_job: str | None = None


class OverrideReq(BaseModel):
    radio: dict[str, BandChangeReq | dict] = {}
    note: str = ""


# ---------------------------------------------------------------- app

def create_app(state: AppState | None = None, token: str | None = None) -> FastAPI:
    state = state or AppState()
    token = token or secrets.token_urlsafe(24)
    app = FastAPI(title="AREDN Fleet Manager", version=__version__, docs_url=None, redoc_url=None)
    app.state.fm = state
    app.state.token = token

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            supplied = request.cookies.get(COOKIE) or request.headers.get("x-fleet-token")
            if not supplied or not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "not authorised; open the link printed at startup"},
                                    status_code=401)
        return await call_next(request)

    @app.get("/")
    async def index(request: Request):
        t = request.query_params.get("token")
        if t:
            if not secrets.compare_digest(t, token):
                raise HTTPException(401, "bad token")
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, t, httponly=True, samesite="strict")
            return resp
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def fingerprint(obj: Any) -> str:
        return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()

    def start(kind: str, title: str, mutating: bool, body, fp: str = "",
              dry_run: bool = False) -> dict[str, Any]:
        try:
            return state.jobs.start(kind, title, mutating, body, fp, dry_run).summary()
        except JobBusy as exc:
            raise HTTPException(409, str(exc)) from exc

    # ------------------------------------------------------------ info
    @app.get("/api/info")
    async def info():
        return {"version": __version__, "portable": paths.portable_root() is not None,
                "config_dir": str(state.config_dir), "firmware_dir": str(state.fw_dir),
                "active_job": (j.summary() if (j := state.jobs.active_mutating()) else None)}

    # ------------------------------------------------------------ discovery
    @app.get("/api/topology")
    async def topology():
        return state.topology_view()

    @app.post("/api/discover")
    async def discover(req: DiscoverReq):
        async def body(job: Job):
            job.log(f"Crawling the mesh from {req.start}…")
            res = await asyncio.to_thread(crawl, req.start)
            state.set_discovery(res.topology, res.nodes, res.errors, res.start)
            await tiny_check(mark_tiny_builds, list(res.nodes))
            job.log(f"Found {len(res.nodes)} nodes and {len(res.topology.links)} links; "
                    f"manager attached at {res.start}")
            for n, e in res.errors.items():
                job.log(f"{n}: unreachable ({e})", "warn")
            return {"nodes": len(res.nodes), "links": len(res.topology.links)}
        return start("discover", f"Discover from {req.start}", False, body)

    # ------------------------------------------------------------ credentials
    @app.get("/api/creds")
    async def creds_list():
        store = state.creds()
        return {"credentials": [{"label": c.label, "kind": c.kind, "source": c.source}
                                for c in store.credentials],
                "known_good": store.known_good}

    @app.post("/api/creds")
    async def creds_add(req: CredReq):
        store = state.creds()
        if req.kind == "key":
            if not req.key_path or not Path(req.key_path).expanduser().is_file():
                raise HTTPException(400, f"key file not found: {req.key_path}")
            cred = Credential(req.label, key_path=req.key_path)
        elif req.kind == "password":
            if not req.password:
                raise HTTPException(400, "password is empty")
            try:
                store_password(req.label, req.password)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(500, f"OS credential store unavailable: {exc}") from exc
            cred = Credential(req.label, keyring=True)
        else:
            raise HTTPException(400, "kind must be key or password")
        store.remove(req.label)
        store.credentials.append(cred)
        state.save_creds(store)
        return {"ok": True}

    @app.delete("/api/creds/{label}")
    async def creds_delete(label: str):
        store = state.creds()
        cred = next((c for c in store.credentials if c.label == label), None)
        if not cred:
            raise HTTPException(404, "no such credential")
        if cred.keyring:
            try:
                forget_password(label)
            except Exception:  # noqa: BLE001
                pass
        store.remove(label)
        state.save_creds(store)
        return {"ok": True}

    # ------------------------------------------------------------ firmware
    @app.get("/api/firmware/local")
    async def firmware_local():
        store = FirmwareStore(state.fw_dir)
        out = []
        for v in store.local_versions():
            rel = store.load_release(v)
            have = sum(1 for p in rel.profiles.values() for i in p.images
                       if store.path_for(rel, p, i).exists())
            out.append({"version": v, "channel": rel.channel, "profiles": len(rel.profiles),
                        "images": have})
        return {"releases": out}

    @app.get("/api/firmware/channels")
    async def firmware_channels(stable: int = 2, nightly: bool = True):
        cat = Catalog(state.mirror)
        try:
            versions = await asyncio.to_thread(cat.versions)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"can't reach {state.mirror}: {exc}") from exc
        return {"channels": [c.__dict__ for c in select_channels(versions, stable, nightly)]}

    @app.post("/api/firmware/fetch")
    async def firmware_fetch(req: FetchReq):
        models = sorted({i.get("model", "") for i in state.nodes.values() if i.get("model")})
        if req.scope == "mesh" and not models:
            raise HTTPException(400, "no discovered nodes yet; run Discover or choose All hardware")

        async def body(job: Job):
            cat = Catalog(state.mirror)
            store = FirmwareStore(state.fw_dir)
            kinds = ["sysupgrade"] + (["factory"] if req.include_factory else [])
            versions = await asyncio.to_thread(cat.versions)
            if req.version:
                if req.version not in versions:
                    raise ValueError(f"{req.version} isn't in AREDN's firmware catalog")
                chans = [Channel("release", req.version, versions[req.version])]
            else:
                chans = select_channels(versions, req.stable, req.nightly)
            for chan in chans:
                if job.stop_requested:
                    break
                boards = req.boards if req.scope == "board" else None
                job.log(f"{chan.name} {chan.version}: reading hardware list…")
                rel = await asyncio.to_thread(cat.release, chan, None, boards)
                if req.scope == "mesh":
                    keep = {p.id for m in models for p in match_profile(rel, model=m)}
                    rel.profiles = {k: v for k, v in rel.profiles.items() if k in keep}
                    for m in models:
                        if not match_profile(rel, model=m):
                            job.log(f"no image matched model {m!r} in {chan.version}", "warn")
                job.log(f"{chan.name} {rel.version}: {len(rel.profiles)} hardware profile(s)")
                counts = {"downloaded": 0, "cached": 0, "failed": 0}

                def progress(r):
                    counts[r.status] += 1
                    if r.status == "failed":
                        job.log(f"FAILED {r.path.name}: {r.error}", "error")
                    elif r.status == "downloaded":
                        job.log(f"downloaded {r.path.name}")

                await asyncio.to_thread(store.fetch_release, rel, kinds, 4, progress)
                job.log(f"{chan.name} {rel.version}: downloaded {counts['downloaded']}, "
                        f"cached {counts['cached']}, failed {counts['failed']}",
                        "error" if counts["failed"] else "info")
        return start("firmware", "Fetch firmware", False, body)

    # ------------------------------------------------------------ plan / upgrade
    def ptmp_order(nodes: list[str]) -> list[tuple[str, str]]:
        # AREDN RC notes: a PtMP access point must be upgraded before its Mesh Stations.
        from ..rc import ptmp_pairs
        sel = set(nodes)
        return [(a, b) for a, b in ptmp_pairs(state.topology.links, state.radios) if a in sel and b in sel]

    def make_plan(req: PlanReq):
        if not state.root:
            raise HTTPException(400, "run Discover first")
        try:
            return plan_rollout(state.topology, state.root, req.nodes, req.parallel,
                                ("TUN",) if req.exclude_tunnels else (), ptmp_order(req.nodes))
        except PlanError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/plan")
    async def plan(req: PlanReq):
        p = make_plan(req)
        return {"root": p.root, "waves": p.waves, "warnings": p.warnings,
                "unreachable": sorted(p.unreachable),
                "distances": {n: p.distances.get(n) for w in p.waves for n in w}}

    @app.post("/api/upgrade")
    async def upgrade(req: UpgradeReq):
        if req.execute and req.confirm.strip().lower() != "upgrade":
            raise HTTPException(400, "type 'upgrade' to confirm")
        fp = fingerprint({"nodes": sorted(req.nodes), "version": req.version, "parallel": req.parallel,
                          "soak": req.soak, "tunnels": req.exclude_tunnels, "root": state.root,
                          "order": ptmp_order(req.nodes)})
        if req.execute and (why := state.jobs.reviewed(req.reviewed_job, "upgrade", fp)):
            raise HTTPException(409, why)
        p = make_plan(req)
        store = FirmwareStore(state.fw_dir)
        if req.version not in store.local_versions():
            raise HTTPException(400, f"{req.version} isn't downloaded; fetch it on the Firmware tab")
        release = store.load_release(req.version)

        from ..rc import is_rc_or_later
        rc = is_rc_or_later(release.version)
        backup_dir = state.config_dir / "backups"

        async def body(job: Job):
            from ..jobs.upgrade import UpgradeConfig, run_rollout

            creds = state.creds()
            no_radios = sorted(n for n in req.nodes if n not in state.radios or state.radios_stale(n))
            extra_warn = []
            if rc and no_radios:
                extra_warn.append("Radio details missing or out of date for " + ", ".join(no_radios)
                                  + ": PtMP access points can't be ordered before their stations "
                                    "until radios are read (stations are blocked rather than risked).")
            job.log(f"{'UPGRADE' if req.execute else 'Dry run'} to {release.version}: "
                    + " → ".join(f"[{', '.join(w)}]" for w in p.waves))
            for w in p.warnings:
                job.log(w, "warn")

            def on_result(r):
                lvl = {"failed": "error", "ok": "ok", "skipped": "info"}.get(r.status, "info")
                job.log(f"{r.node}: {r.status} {r.detail}", lvl, node=r.node, status=r.status)
                for n in r.notes:
                    job.log(f"{r.node}: {n}", "warn")
                if r.backup:
                    job.log(f"{r.node}: configuration backed up to {r.backup}")
                if r.status == "ok":
                    state.set_firmware(r.node, r.to_version)

            cfg = UpgradeConfig(soak_seconds=req.soak, backup_dir=backup_dir, rc_checks=rc)
            from ..rc import ptmp_pairs
            from ..routing import LABELS, migration_blocks, routing_of
            aps: dict[str, set[str]] = {}
            for ap, sta in ptmp_pairs(state.topology.links, state.radios):
                aps.setdefault(sta, set()).add(ap)
            # OLSR -> Babel: judged on the whole mesh (each node's protocol from its firmware).
            # Nodes discovery couldn't read have no firmware on record; ask them now, so the
            # routing check is based on what they run rather than on a gap.
            unknown_fw = [n for n in state.topology.nodes
                          if not (state.nodes.get(n) or {}).get("firmware")]
            if unknown_fw:
                infos = await asyncio.gather(*(state.sysinfo(n) for n in unknown_fw))
                for n, si in zip(unknown_fw, infos):
                    fw = ((si or {}).get("node_details") or {}).get("firmware_version")
                    if fw:
                        state.nodes.setdefault(n, {})["firmware"] = str(fw)
                        model = ((si or {}).get("node_details") or {}).get("model")
                        if model and not state.nodes[n].get("model"):
                            state.nodes[n]["model"] = str(model)
                        state.errors.pop(n, None)
                        job.log(f"{n}: firmware {fw} (read now; discovery couldn't)")
                state.save_topology()
            await tiny_check(mark_tiny_builds, list(state.topology.nodes))
            protocols = {n: routing_of((state.nodes.get(n) or {}).get("firmware", ""),
                                       n not in state.no_babel)
                         for n in state.topology.nodes}
            blocked = migration_blocks(state.topology, state.root or "", req.nodes, release.version,
                                       protocols, ("TUN",) if req.exclude_tunnels else (),
                                       versions={n: (state.nodes.get(n) or {}).get("firmware", "")
                                                 for n in state.topology.nodes})
            for n, why in sorted(blocked.items()):
                job.log(f"{n}: {why}", "warn")
            index = ReleaseIndex(Catalog(state.mirror), state.fw_dir / "catalog")
            rep = await run_rollout(
                p, release, store, state.connect_factory(creds),
                state.sysinfo, cfg, execute=req.execute,
                audit=state.audit_path, on_result=on_result,
                should_stop=lambda: job.stop_requested, on_inventory=state.set_radios,
                access_points=aps, blocked=blocked, final_release=index.final_release)
            state.save_creds(creds)
            if rep.stopped_early:
                job.status = "stopped" if job.stop_requested else "failed"
                job.log(rep.reason, "error")
            else:
                job.log("Rollout finished" if req.execute else "Dry run finished; nothing changed", "ok")
            wave_of = {n: i + 1 for i, w in enumerate(p.waves) for n in w}
            models = {n: (state.nodes.get(n) or {}).get("model", "") for n in wave_of}
            rows = [{**asdict(r), "wave": wave_of.get(r.node), "model": models.get(r.node, ""),
                     "routing_label": LABELS.get(r.routing or protocols.get(r.node, ""), "")}
                    for r in rep.results]
            return {"version": release.version, "channel": release.channel, "waves": p.waves,
                    "warnings": extra_warn + p.warnings, "unreachable": sorted(p.unreachable),
                    "results": rows, "rc": rc, "backup_dir": str(backup_dir),
                    "ptmp_order": ptmp_order(req.nodes),
                    "target_routing": LABELS[routing_of(release.version)],
                    "reason": rep.reason, "blocked": [r["node"] for r in rows if r["status"] == "failed"]}

        title = f"{'Upgrade' if req.execute else 'Dry run'} → {release.version} ({len(req.nodes)} nodes)"
        return start("upgrade", title, req.execute, body, fp, dry_run=not req.execute)

    # ------------------------------------------------------------ radio
    @app.post("/api/radios/read")
    async def radios_read(req: NodesReq):
        from ..jobs.radio import describe, read_node_radios

        if not req.nodes:
            raise HTTPException(400, "select at least one node")

        async def body(job: Job):
            creds = state.creds()
            connect = state.connect_factory(creds)
            sem = asyncio.Semaphore(4)
            ok = 0

            async def one(n):
                nonlocal ok
                async with sem:
                    try:
                        s = await connect(n)
                        try:
                            inv = await read_node_radios(s)
                        finally:
                            await s.close()
                    except Exception as exc:  # noqa: BLE001
                        job.log(f"{n}: couldn't read radios ({errtext(exc)})", "error")
                        return
                    state.set_radios(n, inv)
                    ok += 1
                    job.log(f"{n}: {describe(inv)}"
                            + (" (older firmware: shown only; upgrade to change radios)" if inv.legacy else ""), "ok")

            job.log(f"Reading radio details from {len(req.nodes)} node(s) over SSH…")
            await asyncio.gather(*(one(n) for n in req.nodes))
            state.save_creds(creds)
            job.log(f"Read {ok} of {len(req.nodes)} node(s)", "ok" if ok == len(req.nodes) else "warn")
            if not ok:
                raise RuntimeError("no radios read; see the Activity log")
        return start("radios", f"Read radios ({len(req.nodes)} nodes)", False, body)

    @app.post("/api/radio/options")
    async def radio_opts(req: NodesReq):
        old = [n for n in req.nodes if old_firmware_reason(n)]
        usable = [n for n in req.nodes if n not in old]
        stale = {n for n in usable if state.radios_stale(n)}
        out = radio_options(state.radios, usable, stale)
        # Older nodes can still be read (view only), so offer Read radios for them too.
        out["missing"] = list(out.get("missing") or []) + [
            n for n in old if n not in state.radios or state.radios_stale(n)]
        out["too_old"] = [{"node": n, "firmware": (state.nodes.get(n) or {}).get("firmware", "")}
                          for n in old]
        return out

    @app.post("/api/radio")
    async def radio(req: RadioReq):
        from ..jobs.radio import (BandChange, RadioConfig, cluster_warnings, rf_clusters,
                                  run_radio_change, validate_changes)
        from ..jobs.upgrade import AuditLog

        if req.execute and req.confirm.strip().lower() != "switch":
            raise HTTPException(400, "type 'switch' to confirm")
        from ..jobs.radio import merge_changes

        group = [BandChange(c.band, c.mode, c.channel, c.width, c.peer, c.ssid) for c in req.changes]
        per_node = {n: merge_changes(group, (state.overrides.get(n) or {}).get("radio"))
                    for n in req.nodes}
        if not any(per_node.values()):
            raise HTTPException(400, "nothing to change: choose a setting, or give nodes their own settings")
        try:
            for lst in per_node.values():
                if lst:
                    validate_changes(lst)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        selected = set(req.nodes)
        clusters = [sorted(c & selected) for c in rf_clusters(state.topology, selected)]
        fp = fingerprint({"nodes": sorted(req.nodes), "changes": [c.model_dump() for c in req.changes],
                          "own": {n: (state.overrides.get(n) or {}).get("radio") for n in sorted(req.nodes)},
                          "revert_after": req.revert_after})
        if req.execute and (why := state.jobs.reviewed(req.reviewed_job, "radio", fp)):
            raise HTTPException(409, why)

        async def body(job: Job):
            creds = state.creds()
            out = {"clusters": [], "warnings": cluster_warnings(state.topology, selected), "blocked": []}
            for w in out["warnings"]:
                job.log(w, "warn")
            for n in sorted(selected):
                if state.overrides.get(n):
                    from .state import custom_summary
                    job.log(f"{n} has its own settings: {custom_summary(state.overrides[n])['text']}")
            log = AuditLog(state.audit_path)
            for i, c in enumerate(clusters, 1):
                if job.stop_requested:
                    job.log("stopped before the next cluster", "warn")
                    break
                job.log(f"RF cluster {i}: {', '.join(c)}")
                if not any(per_node[n] for n in c):
                    job.log(f"RF cluster {i}: nothing to change")
                    out["clusters"].append({"index": i, "nodes": [
                        {"node": n, "status": "unchanged", "detail": "no settings to apply", "ledger": [],
                         "lost_links": [], "own": bool(state.overrides.get(n))} for n in c],
                        "reason": "nothing to change"})
                    continue
                rep = await run_radio_change(
                    c, {n: per_node[n] for n in c}, state.connect_factory(creds), state.sysinfo,
                    RadioConfig(revert_after=req.revert_after), execute=req.execute, log=log,
                    on_inventory=state.set_radios)
                for r in rep.results.values():
                    lvl = {"failed": "error", "confirmed": "ok", "unchanged": "info"}.get(r.status, "info")
                    job.log(f"{r.node}: {r.status} — {r.detail or r.before}", lvl)
                out["clusters"].append({"index": i, "reason": rep.reason, "confirmed": rep.confirmed,
                                        "nodes": [{**asdict(r), "own": bool(state.overrides.get(r.node))}
                                                  for r in rep.results.values()]})
                out["blocked"] += [r.node for r in rep.results.values() if r.status == "failed"]
                if rep.confirmed:
                    job.log(f"cluster {i} confirmed on the new settings", "ok")
                elif rep.reason in ("dry run", "nothing to change on these nodes"):
                    job.log(rep.reason, "info")
                else:
                    job.log(rep.reason, "error")
                    if req.execute:
                        job.status = "failed"
                        break
            state.save_creds(creds)
            return out

        title = f"{'Radio change' if req.execute else 'Radio dry run'} ({len(req.nodes)} nodes)"
        return start("radio", title, req.execute, body, fp, dry_run=not req.execute)

    async def tiny_check(fn, names) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(fn, names), 30)
        except Exception:  # noqa: BLE001 - slow or offline catalog: labels stay as they were
            pass

    def mark_tiny_builds(names: list[str]) -> None:
        """Note hardware whose newest release is below 4.0 (tiny builds have no Babel).
        Uses the release catalog (cached on disk); if it can't be read, nothing is marked."""
        from ..routing import DUAL, routing_of, tiny_build
        # Only 3.25.5-3.25.10 nodes are in doubt (full build: OLSR+Babel; tiny build: OLSR).
        names = [n for n in names
                 if routing_of((state.nodes.get(n) or {}).get("firmware", "")) == DUAL]
        if not names:
            return
        try:
            index = ReleaseIndex(Catalog(state.mirror), state.fw_dir / "catalog")
        except Exception:  # noqa: BLE001
            return
        for n in names:
            info = state.nodes.get(n) or {}
            board = str(info.get("board_id") or "")
            try:
                last = index.final_release(board if "," in board else "", str(info.get("model") or ""))
            except Exception:  # noqa: BLE001 - offline: leave as is
                continue
            if tiny_build(last):
                state.no_babel.add(n)
            else:
                state.no_babel.discard(n)

    def old_firmware_reason(n: str) -> str | None:
        """Radio and settings jobs need the ucode web UI (3.25.5 and later); say so plainly."""
        from ..routing import OLSR, routing_of
        fw = (state.nodes.get(n) or {}).get("firmware", "")
        if routing_of(fw) == OLSR:
            return (f"{n}: firmware {fw} is older than radio changes support (its web UI is Lua, "
                    "not ucode). Upgrade it to an OLSR + Babel release first.")
        return None

    # ------------------------------------------------------------ new device (first install)
    def _catalog_json(cat: Catalog, version: str, path: str) -> dict:
        """Stable releases never change: cache their catalog files on disk."""
        from ..firmware import STABLE_RE
        if STABLE_RE.match(version):
            return ReleaseIndex(cat, state.fw_dir / "catalog")._cached(version, path)
        return cat._json(path)

    @app.get("/api/install/releases")
    async def install_releases():
        from ..firmware import NIGHTLY_RE, STABLE_RE, _stable_key
        cat = Catalog(state.mirror)
        try:
            versions = await asyncio.to_thread(cat.versions)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"can't reach {state.mirror}: {exc}") from exc
        stable = sorted((v for v in versions if STABLE_RE.match(v)), key=_stable_key, reverse=True)
        nightly = sorted((v for v in versions if NIGHTLY_RE.match(v)), reverse=True)[:1]
        return {"releases": stable + nightly, "default": stable[0] if stable else None}

    @app.get("/api/install/hardware")
    async def install_hardware(version: str):
        from ..firmware import _title
        from ..install import method_for
        cat = Catalog(state.mirror)
        versions = await asyncio.to_thread(cat.versions)
        if version not in versions:
            raise HTTPException(404, f"{version} isn't in AREDN's catalog")
        ov = await asyncio.to_thread(_catalog_json, cat, version,
                                     f"afs/www/{versions[version]}/overview.json")
        rows = []
        for p in ov.get("profiles", []):
            titles = [_title(t) for t in p.get("titles", [])]
            m = method_for(p["id"], p.get("target", ""), titles)
            rows.append({"id": p["id"], "target": p.get("target", ""), "titles": titles,
                         "method": m.key, "method_name": m.name})
        rows.sort(key=lambda r: (r["titles"][0] if r["titles"] else r["id"]).lower())
        return {"version": version, "hardware": rows}

    @app.post("/api/install/prepare")
    async def install_prepare(req: InstallReq):
        from ..firmware import Channel, FirmwareStore
        from ..install import method_for, pick_images, stage_files

        async def body(job: Job):
            cat = Catalog(state.mirror)
            versions = await asyncio.to_thread(cat.versions)
            if req.version not in versions:
                raise ValueError(f"{req.version} isn't in AREDN's catalog")
            rel = await asyncio.to_thread(cat.release, Channel("install", req.version, versions[req.version]),
                                          [req.profile])
            prof = rel.profiles.get(req.profile)
            if not prof:
                raise ValueError(f"{req.profile} isn't in {req.version}")
            method = method_for(prof.id, prof.target, prof.titles)
            use, other = pick_images(method, [i.type for i in prof.images])
            missing = [t for t in method.images if t not in use]
            if missing:
                job.log(f"AREDN publishes no {', '.join(missing)} image for this hardware in "
                        f"{req.version}; follow AREDN's guide for it.", "warn")
            store = FirmwareStore(state.fw_dir)
            staged: list[tuple[str, Path]] = []
            for kind in use:
                img = prof.image(kind)
                job.log(f"Downloading {img.name}…")
                r = await asyncio.to_thread(store.fetch, rel, prof, img)
                if r.status == "failed":
                    raise ValueError(f"{img.name}: {r.error}")
                job.log(f"{img.name}: {r.status}, SHA-256 checked", "ok")
                staged.append((kind, r.path))
            dest = state.config_dir / "install" / f"{prof.id}-{req.version}"
            files = stage_files(staged, method, dest)
            for f in files:
                if f.get("as"):
                    job.log(f"Copied as {f['as']} (the name the device asks for)")
            job.log(f"Ready in {dest}", "ok")
            return {"version": req.version, "profile": prof.id, "titles": prof.titles,
                    "target": prof.target, "method": method.to_json(), "files": files,
                    "other_images": [{"type": i.type, "name": i.name} for i in prof.images
                                     if i.type in other],
                    "folder": str(dest), "guide": GUIDE}

        from ..install import GUIDE
        return start("install", f"Prepare install: {req.profile} {req.version}", False, body)

    @app.get("/api/install/probe")
    async def install_probe(ip: str = "192.168.1.1"):
        """Is there an AREDN node answering at this address yet? (read-only)"""
        if not re.match(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$", ip):
            raise HTTPException(400, "give an IPv4 address")
        from ..discovery import http_fetch
        try:
            si = await asyncio.to_thread(http_fetch, ip, 4.0)
        except Exception as exc:  # noqa: BLE001
            return {"found": False, "detail": str(exc)[:200]}
        d = si.get("node_details") or {}
        return {"found": True, "node": si.get("node", ""), "firmware": d.get("firmware_version", ""),
                "model": d.get("model", "")}

    @app.post("/api/open-folder")
    async def open_folder(req: OpenReq):
        """Show a file or folder in Explorer, only inside Fleet Manager's own folders."""
        target = Path(req.path).resolve()
        roots = [state.config_dir.resolve(), state.fw_dir.resolve()]
        if not any(target == r or r in target.parents for r in roots):
            raise HTTPException(403, "only Fleet Manager's own folders")
        if sys.platform == "win32":
            import subprocess
            args = ["explorer", "/select,", str(target)] if target.is_file() else ["explorer", str(target)]
            subprocess.Popen(args)  # noqa: S603,S607
            return {"opened": True}
        return {"opened": False, "path": str(target)}

    # ------------------------------------------------------------ on/off settings (from node dialogs)
    @app.post("/api/settings/read")
    async def settings_read(req: NodesReq):
        from ..jobs.settings import read_node_settings

        if not req.nodes:
            raise HTTPException(400, "select at least one node")

        async def body(job: Job):
            creds = state.creds()
            connect = state.connect_factory(creds)
            sem = asyncio.Semaphore(4)
            ok = 0

            async def one(n):
                nonlocal ok
                async with sem:
                    try:
                        s = await connect(n)
                        try:
                            ns = await read_node_settings(s)
                        finally:
                            await s.close()
                    except Exception as exc:  # noqa: BLE001
                        job.log(f"{n}: couldn't read settings ({errtext(exc)})", "error")
                        return
                    state.set_settings(n, ns)
                    ok += 1
                    where = "its Advanced Configuration page" if ns.legacy else "its dialogs"
                    job.log(f"{n}: {len(ns.switches)} on/off settings in {where} ({ns.firmware})", "ok")

            job.log(f"Reading settings from {len(req.nodes)} node(s)' own dialogs over SSH…")
            await asyncio.gather(*(one(n) for n in req.nodes))
            state.save_creds(creds)
            job.log(f"Read {ok} of {len(req.nodes)} node(s)", "ok" if ok == len(req.nodes) else "warn")
        return start("settings-read", f"Read settings ({len(req.nodes)} nodes)", False, body)

    @app.post("/api/settings/summary")
    async def settings_summary(req: NodesReq):
        return state.settings_summary(req.nodes)

    @app.post("/api/settings")
    async def settings_apply(req: SettingsReq):
        from ..jobs.settings import SettingChange, apply_node_settings
        from ..jobs.upgrade import AuditLog

        if req.execute and req.confirm.strip().lower() != "apply":
            raise HTTPException(400, "type 'apply' to confirm")
        if not req.nodes or not req.changes:
            raise HTTPException(400, "select nodes and choose at least one setting to change")
        changes = [SettingChange(c.key, c.on) for c in req.changes]
        # Outermost first, one node at a time, the node this computer uses last.
        if state.root:
            plan = make_plan(PlanReq(nodes=req.nodes, parallel=1, exclude_tunnels=False))
            waves = plan.waves
        else:
            waves = []
        order = [n for w in waves for n in w] + sorted(n for n in req.nodes if n not in
                                                       {m for w in waves for m in w})
        fp = fingerprint({"nodes": sorted(req.nodes), "changes": sorted((c.key, c.on) for c in changes)})
        if req.execute and (why := state.jobs.reviewed(req.reviewed_job, "settings", fp)):
            raise HTTPException(409, why)

        async def body(job: Job):
            creds = state.creds()
            connect = state.connect_factory(creds)
            log = AuditLog(state.audit_path)
            out = {"order": order, "results": [], "blocked": []}
            for n in order:
                if job.stop_requested:
                    job.log("stopped before the next node", "warn")
                    break
                r = await apply_node_settings(n, changes, connect, execute=req.execute,
                                              on_read=state.set_settings, log=log)
                out["results"].append(asdict(r))
                lvl = {"failed": "error", "changed": "ok"}.get(r.status, "info")
                job.log(f"{n}: {r.status}{' — ' + r.detail if r.detail else ''}", lvl)
                if r.status == "failed":
                    out["blocked"].append(n)
                    if req.execute:
                        job.status = "failed"
                        job.log("stopped at the first failure", "error")
                        break
            state.save_creds(creds)
            need = [r["node"] for r in out["results"] if r.get("reboot_required")]
            if need:
                job.log("Reboot needed to finish (each node's page says so): " + ", ".join(need)
                        + ". Reboot them when convenient.", "warn")
            out["reboot_needed"] = need
            return out

        title = f"{'Settings change' if req.execute else 'Settings dry run'} ({len(req.nodes)} nodes)"
        return start("settings", title, req.execute, body, fp, dry_run=not req.execute)

    # ------------------------------------------------------------ node-specific settings
    @app.get("/api/overrides")
    async def overrides_list():
        return {"overrides": state.overrides}

    @app.put("/api/overrides/{node}")
    async def overrides_set(node: str, req: OverrideReq):
        from ..jobs.radio import BandChange, NodeRadios, plan_node

        if node not in state.topology.nodes:
            raise HTTPException(404, f"unknown node {node}")
        radio = {}
        changes = []
        for band, v in req.radio.items():
            d = v.model_dump() if hasattr(v, "model_dump") else dict(v)
            d = {k: d.get(k) for k in ("mode", "channel", "width", "ssid", "peer")}
            if all(x in (None, "") for x in d.values()):
                continue
            c = BandChange(band, d["mode"] or None, d["channel"], d["width"], d["peer"] or None,
                           d["ssid"] or None)
            try:
                c.validate()
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            radio[band] = d
            changes.append(c)
        if changes:
            inv = state.radios.get(node)
            if not inv:
                raise HTTPException(400, f"read {node}'s radios first, so its settings can be checked")
            try:
                plan_node(NodeRadios.from_json(inv), changes)   # same checks a real change gets
            except ValueError as exc:
                raise HTTPException(400, f"{node}: {exc}") from exc
        state.set_override(node, radio, req.note)
        return {"ok": True, "custom": state.overrides.get(node)}

    @app.delete("/api/overrides/{node}")
    async def overrides_delete(node: str):
        state.delete_override(node)
        return {"ok": True}

    # ------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    async def jobs():
        return {"jobs": state.jobs.list()}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str, since: int = 0):
        j = state.jobs.jobs.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return {**j.summary(), "events": j.events[since:], "next": len(j.events),
                "result": j.result if j.status != "running" else None}

    @app.get("/api/jobs/{job_id}/log.txt")
    async def job_text(job_id: str):
        from fastapi.responses import PlainTextResponse
        j = state.jobs.jobs.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return PlainTextResponse(j.text())

    @app.post("/api/jobs/{job_id}/save")
    async def job_save(job_id: str):
        """Write the log to the logs folder now and, on Windows, show it in Explorer."""
        j = state.jobs.jobs.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        path = state.jobs.save_log(j)
        if path and sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", str(path)])  # noqa: S603,S607
        return {"path": str(path) if path else None}

    @app.post("/api/jobs/{job_id}/stop")
    async def job_stop(job_id: str):
        j = state.jobs.jobs.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        j.stop_requested = True
        j.log("Stop requested: finishing the current step first (a node mid-flash is never "
              "interrupted)", "warn")
        return j.summary()

    return app
