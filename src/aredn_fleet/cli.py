"""Command-line entry point: ``aredn-fleet``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .credentials import Credential, CredentialStore
from .discovery import crawl, http_fetch
from .firmware import DEFAULT_BASE, Catalog, FirmwareStore, select_channels
from .topology import Topology, plan_rollout

from . import paths

CONFIG_DIR = paths.config_dir()
FW_DIR = paths.firmware_dir()
CREDS = paths.credentials_file()


def _load_snapshot(path: Path) -> Topology:
    data = json.loads(path.read_text())
    topo = Topology()
    for n in data.get("nodes", []):
        topo.nodes.add(n)
    for link in data.get("links", []):
        topo.add_link(link["a"], link["b"], link.get("kind", "RF"))
    return topo


# ---------------------------------------------------------------- discover / plan

def cmd_discover(args) -> int:
    res = crawl(args.start)
    snapshot = {
        "nodes": sorted(res.topology.nodes),
        "links": [l.__dict__ for l in res.topology.links],
        "info": {n: {"firmware": i.firmware, "model": i.model} for n, i in res.nodes.items()},
        "errors": res.errors,
    }
    out = json.dumps(snapshot, indent=2)
    if args.output:
        Path(args.output).write_text(out)
        print(f"{len(res.nodes)} nodes, {len(res.topology.links)} links -> {args.output}")
    else:
        print(out)
    return 0


def _plan(args):
    topo = _load_snapshot(Path(args.snapshot))
    selected = args.nodes or sorted(topo.nodes)
    return plan_rollout(topo, args.root, selected, max_parallel=args.parallel,
                        exclude_link_kinds=args.exclude or ())


def cmd_plan(args) -> int:
    print(_plan(args).describe())
    return 0


# ---------------------------------------------------------------- firmware

def cmd_fw_channels(args) -> int:
    cat = Catalog(args.mirror)
    for c in select_channels(cat.versions(), args.stable, not args.no_nightly):
        print(f"{c.name:10} {c.version}")
    return 0


def cmd_fw_fetch(args) -> int:
    cat = Catalog(args.mirror)
    store = FirmwareStore(Path(args.dir))
    chans = select_channels(cat.versions(), args.stable, not args.no_nightly)
    kinds = ["sysupgrade"] + (["factory"] if args.include_factory else [])
    rc = 0
    for chan in chans:
        rel = cat.release(chan, args.profiles, args.board)
        print(f"{chan.name} {rel.version}: {len(rel.profiles)} hardware profiles", flush=True)
        if args.metadata_only:
            store.write_manifest(rel)
            continue
        results = store.fetch_release(rel, kinds, workers=args.workers)
        counts = {s: sum(r.status == s for r in results) for s in ("downloaded", "cached", "failed")}
        print(f"  downloaded {counts['downloaded']}, cached {counts['cached']}, failed {counts['failed']}")
        for r in results:
            if r.status == "failed":
                rc = 1
                print(f"  FAILED {r.path.name}: {r.error}")
    return rc


def cmd_fw_list(args) -> int:
    store = FirmwareStore(Path(args.dir))
    for v in store.local_versions():
        rel = store.load_release(v)
        have = sum(1 for p in rel.profiles.values() for i in p.images
                   if store.path_for(rel, p, i).exists())
        print(f"{rel.channel:10} {v:22} {len(rel.profiles):4} profiles, {have} images on disk")
    return 0


# ---------------------------------------------------------------- credentials

def cmd_creds_add(args) -> int:
    store = CredentialStore.load(CREDS)
    if not (args.key or args.password_env) or (args.key and args.password_env):
        print("give exactly one of --key or --password-env", file=sys.stderr)
        return 2
    store.credentials = [c for c in store.credentials if c.label != args.label]
    store.credentials.append(Credential(args.label, args.key, args.password_env))
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    store.save(CREDS)
    print(f"saved {args.label} ({'key' if args.key else 'password from $' + args.password_env})")
    return 0


def cmd_creds_list(args) -> int:
    store = CredentialStore.load(CREDS)
    for c in store.credentials:
        print(f"{c.label:16} {c.kind:9} {c.key_path or '$' + (c.password_env or '')}")
    for node, label in sorted(store.known_good.items()):
        print(f"  {node} -> {label}")
    return 0


# ---------------------------------------------------------------- upgrade

def cmd_upgrade(args) -> int:
    from .jobs.upgrade import UpgradeConfig, run_rollout
    from .transport import connect

    plan = _plan(args)
    store = FirmwareStore(Path(args.dir))
    if args.version not in store.local_versions():
        have = ", ".join(store.local_versions()) or "none"
        print(f"{args.version} is not downloaded (have: {have}). Run `aredn-fleet firmware fetch`.",
              file=sys.stderr)
        return 2
    release = store.load_release(args.version)
    creds = CredentialStore.load(CREDS)
    print(plan.describe())
    print(f"\nTarget firmware: {release.version} ({release.channel})")

    if args.execute and not args.yes:
        if input("Type 'upgrade' to start the rollout: ").strip() != "upgrade":
            print("aborted")
            return 1

    async def sysinfo(node):
        try:
            return await asyncio.to_thread(http_fetch, node, 8.0)
        except Exception:  # noqa: BLE001
            return None

    def show(r):
        print(f"[{r.status:13}] {r.node:24} {r.profile:24} {r.detail}", flush=True)

    cfg = UpgradeConfig(soak_seconds=args.soak)
    report = asyncio.run(run_rollout(
        plan, release, store, lambda n: connect(n, creds), sysinfo, cfg,
        execute=args.execute, audit=Path(args.audit) if args.audit else None, on_result=show))
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    creds.save(CREDS)
    if report.stopped_early:
        print(f"\nSTOPPED: {report.reason}")
        return 1
    if not args.execute:
        print("\nDry run only. Re-run with --execute to upgrade.")
    return 0


# ---------------------------------------------------------------- radio

def cmd_radio(args) -> int:
    from .jobs.radio import (BandChange, RadioConfig, cluster_warnings, rf_clusters,
                             run_radio_change, validate_changes)
    from .jobs.upgrade import AuditLog
    from .transport import connect

    topo = _load_snapshot(Path(args.snapshot))
    selected = set(args.nodes or topo.nodes)
    change = BandChange(args.band, args.mode, args.channel, args.width, args.peer)
    validate_changes([change])
    for w in cluster_warnings(topo, selected):
        print("WARNING:", w)
    clusters = [sorted(c & selected) for c in rf_clusters(topo, selected)]
    print(f"Change: {change}")
    for i, c in enumerate(clusters, 1):
        print(f"RF cluster {i}: {', '.join(c)} (switch together)")
    if args.execute and not args.yes:
        if input("Type 'switch' to arm the change: ").strip() != "switch":
            print("aborted")
            return 1

    creds = CredentialStore.load(CREDS)

    async def sysinfo(node):
        try:
            return await asyncio.to_thread(http_fetch, node, 8.0)
        except Exception:  # noqa: BLE001
            return None

    cfg = RadioConfig(lead_seconds=args.lead, revert_after=args.revert_after)
    log = AuditLog(Path(args.audit) if args.audit else None)
    rc = 0
    for c in clusters:
        rep = asyncio.run(run_radio_change(c, [change], lambda n: connect(n, creds), sysinfo, cfg,
                                           execute=args.execute, log=log))
        for r in rep.results.values():
            print(f"[{r.status:12}] {r.node:24} {r.before:34} {r.detail}")
        print(f"-> {'CONFIRMED' if rep.confirmed else rep.reason}")
        if args.execute and not rep.confirmed:
            rc = 1
            break  # don't touch further clusters after a failure
    creds.save(CREDS)
    return rc


# ---------------------------------------------------------------- main

def _plan_args(p):
    p.add_argument("snapshot", help="topology snapshot from `aredn-fleet discover`")
    p.add_argument("--root", required=True, help="node the manager is attached to")
    p.add_argument("--nodes", nargs="*", help="selected nodes (default: all)")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--exclude", nargs="*", help="link kinds to ignore, e.g. TUN")


def _fw_source_args(p):
    p.add_argument("--mirror", default=DEFAULT_BASE, help="download base URL or local mirror")
    p.add_argument("--stable", type=int, default=2, help="how many newest stable releases")
    p.add_argument("--no-nightly", action="store_true")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aredn-fleet", description="AREDN Fleet Manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="crawl the mesh and save a topology snapshot")
    d.add_argument("--start", default="localnode")
    d.add_argument("-o", "--output")
    d.set_defaults(func=cmd_discover)

    pl = sub.add_parser("plan", help="show the edge-inward rollout order (dry run)")
    _plan_args(pl)
    pl.set_defaults(func=cmd_plan)

    fw = sub.add_parser("firmware", help="firmware catalog and downloads").add_subparsers(
        dest="fwcmd", required=True)
    c = fw.add_parser("channels", help="show the releases that would be fetched")
    _fw_source_args(c)
    c.set_defaults(func=cmd_fw_channels)
    f = fw.add_parser("fetch", help="download images for every hardware profile (or --profiles)")
    _fw_source_args(f)
    f.add_argument("--profiles", nargs="*", help="limit to these profile ids")
    f.add_argument("--board", nargs="*", help="only hardware matching these OpenWrt board names, "
                   "e.g. mikrotik,routerboard-952ui-5ac2nd (cat /tmp/sysinfo/board_name on a node)")
    f.add_argument("--include-factory", action="store_true", help="also fetch first-install images")
    f.add_argument("--metadata-only", action="store_true", help="catalog only, no image downloads")
    f.add_argument("--workers", type=int, default=4)
    f.add_argument("--dir", default=str(FW_DIR))
    f.set_defaults(func=cmd_fw_fetch)
    li = fw.add_parser("list", help="show downloaded releases")
    li.add_argument("--dir", default=str(FW_DIR))
    li.set_defaults(func=cmd_fw_list)

    cr = sub.add_parser("creds", help="manage login credentials").add_subparsers(
        dest="crcmd", required=True)
    a = cr.add_parser("add", help="add an SSH key or a password (by env var name)")
    a.add_argument("label")
    a.add_argument("--key", help="path to a private key")
    a.add_argument("--password-env", help="environment variable that holds the password")
    a.set_defaults(func=cmd_creds_add)
    cr.add_parser("list").set_defaults(func=cmd_creds_list)

    u = sub.add_parser("upgrade", help="edge-inward firmware rollout (dry run unless --execute)")
    _plan_args(u)
    u.add_argument("--version", required=True, help="downloaded version, e.g. 4.26.10.0")
    u.add_argument("--dir", default=str(FW_DIR))
    u.add_argument("--soak", type=float, default=180, help="seconds to wait between waves")
    u.add_argument("--audit", default="aredn-fleet-audit.jsonl")
    u.add_argument("--execute", action="store_true")
    u.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    u.set_defaults(func=cmd_upgrade)

    ra = sub.add_parser("radio", help="change mesh channel/width/mode per RF cluster, with "
                        "auto-revert (dry run unless --execute)")
    ra.add_argument("snapshot")
    ra.add_argument("--nodes", nargs="*", help="selected nodes (default: all)")
    ra.add_argument("--band", required=True, help="which radio: 5GHz, 2.4GHz, 900MHz, 3GHz")
    ra.add_argument("--channel", type=int)
    ra.add_argument("--width", type=int, help="channel width in MHz (5, 10, 20, ...)")
    ra.add_argument("--mode", choices=["mesh", "meshap", "meshptp", "meshsta", "off"])
    ra.add_argument("--peer", help="peer MAC for meshptp")
    ra.add_argument("--lead", type=float, default=90, help="seconds from arming to the switch")
    ra.add_argument("--revert-after", type=int, default=600,
                    help="nodes revert unless confirmed within this many seconds of the switch")
    ra.add_argument("--audit", default="aredn-fleet-audit.jsonl")
    ra.add_argument("--execute", action="store_true")
    ra.add_argument("--yes", action="store_true")
    ra.set_defaults(func=cmd_radio)

    g = sub.add_parser("gui", help="open the desktop app")
    g.set_defaults(func=lambda a: __import__("aredn_fleet.desktop", fromlist=["main"]).main())
    sv = sub.add_parser("serve", help="run the web UI headless (e.g. on a Pi on the mesh)")
    sv.add_argument("--host", default="127.0.0.1",
                    help="address to listen on; 0.0.0.0 to allow other machines")
    sv.add_argument("--port", type=int, default=8787)
    sv.set_defaults(func=lambda a: __import__("aredn_fleet.desktop", fromlist=["serve"]).serve(
        a.host, a.port))

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
