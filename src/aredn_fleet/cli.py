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

CONFIG_DIR = Path.home() / ".config" / "aredn-fleet"
FW_DIR = Path.home() / ".cache" / "aredn-fleet" / "firmware"
CREDS = CONFIG_DIR / "credentials.json"


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
        rel = cat.release(chan, args.profiles)
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

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
