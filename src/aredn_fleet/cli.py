"""Command-line entry point: ``aredn-fleet``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .discovery import crawl
from .topology import Topology, plan_rollout


def _load_snapshot(path: Path) -> Topology:
    data = json.loads(path.read_text())
    topo = Topology()
    for n in data.get("nodes", []):
        topo.nodes.add(n)
    for link in data.get("links", []):
        topo.add_link(link["a"], link["b"], link.get("kind", "RF"))
    return topo


def cmd_discover(args: argparse.Namespace) -> int:
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


def cmd_plan(args: argparse.Namespace) -> int:
    topo = _load_snapshot(Path(args.snapshot))
    selected = args.nodes or sorted(topo.nodes)
    plan = plan_rollout(topo, args.root, selected, max_parallel=args.parallel,
                        exclude_link_kinds=args.exclude or ())
    print(plan.describe())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aredn-fleet", description="AREDN Fleet Manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="crawl the mesh and save a topology snapshot")
    d.add_argument("--start", default="localnode")
    d.add_argument("-o", "--output")
    d.set_defaults(func=cmd_discover)

    pl = sub.add_parser("plan", help="show the edge-inward rollout order (dry run)")
    pl.add_argument("snapshot")
    pl.add_argument("--root", required=True, help="node the manager is attached to")
    pl.add_argument("--nodes", nargs="*", help="selected nodes (default: all)")
    pl.add_argument("--parallel", type=int, default=1)
    pl.add_argument("--exclude", nargs="*", help="link kinds to ignore, e.g. TUN")
    pl.set_defaults(func=cmd_plan)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
