"""Which routing protocol a node speaks, and moving a mixed mesh from OLSR to Babel safely.

AREDN history (release notes):

* up to 3.25.4: OLSR only;
* 3.25.5.0 – 3.25.10.0: OLSR and Babel side by side ("dual");
* 4.26.1.0 and later: Babel only.

Exception: "tiny" builds (32 MB nodes such as the NanoStation M XM) never got Babel ("Tiny builds
do not support Babel", 3.25.5.0 notes); their last release is 3.25.5.1, and they are OLSR-only
on every release. Such a node's newest release is below 4.0, which is how they're recognised.

Babel-only nodes don't know about OLSR-only nodes, so a node that stops speaking OLSR can
strand OLSR-only nodes that reach the rest of the mesh through it, and an OLSR-only node can't
jump straight to Babel-only without losing its neighbours. The safe order is: everything to a
dual-protocol release first, then to Babel-only, outermost first.

The version gives the protocol for releases. Nightlies are labelled by date, so most of them
are "unknown" until a login shows which daemons run (ROUTING_CMD).
"""
from __future__ import annotations

import re
from collections import deque
from typing import Iterable

from .topology import Topology

OLSR, DUAL, BABEL, UNKNOWN = "olsr", "dual", "babel", "unknown"
LABELS = {OLSR: "OLSR", DUAL: "OLSR + Babel", BABEL: "Babel", UNKNOWN: "unknown"}
FIRST_DUAL = (3, 25, 5, 0)
FIRST_BABEL_ONLY = (4, 0, 0, 0)
# 4.26.1.0, the first Babel-only release, came out 2026-01-14; regular nightlies after it are
# Babel-only. Nightlies named "babel-…" always were.
BABEL_NIGHTLIES_FROM = "20260114"

STABLE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")
NIGHTLY_RE = re.compile(r"^(babel-)?(\d{8})-[0-9a-f]+$")

# Read-only: which routing daemons run right now.
ROUTING_CMD = ("pidof olsrd >/dev/null 2>&1 && echo olsrd; "
               "pidof babeld >/dev/null 2>&1 && echo babeld; true")


def routing_of(version: str, babel_build: bool = True) -> str:
    """``babel_build=False`` for hardware that only gets tiny builds (no Babel on any release)."""
    v = (version or "").strip()
    if m := STABLE_RE.match(v):
        t = tuple(int(x) for x in m.groups())
        if not babel_build:
            return OLSR
        return OLSR if t < FIRST_DUAL else DUAL if t < FIRST_BABEL_ONLY else BABEL
    if m := NIGHTLY_RE.match(v):
        if m.group(1) or m.group(2) >= BABEL_NIGHTLIES_FROM:
            return BABEL
        return UNKNOWN
    return UNKNOWN


def tiny_build(final_release: str | None) -> bool:
    """True when the hardware's newest release predates Babel-only firmware (tiny builds)."""
    m = STABLE_RE.match(final_release or "")
    return bool(m) and tuple(int(x) for x in m.groups()) < FIRST_BABEL_ONLY


def routing_from_daemons(out: str) -> str | None:
    """From ROUTING_CMD output; None if neither daemon runs (can't tell)."""
    o, b = "olsrd" in out, "babeld" in out
    return DUAL if o and b else OLSR if o else BABEL if b else None


def speaks_babel(p: str) -> bool:
    return p in (DUAL, BABEL)


def speaks_olsr(p: str) -> bool:
    return p in (OLSR, DUAL)


def _reach(topo: Topology, root: str | set[str], ok: set[str],
           exclude_kinds: Iterable[str] = ()) -> set[str]:
    """Nodes reachable from root (or any of several roots) using only nodes in ``ok``."""
    roots = {root} if isinstance(root, str) else set(root)
    roots &= ok
    if not roots:
        return set()
    ex = set(exclude_kinds)
    adj: dict[str, set[str]] = {}
    for l in topo.links:
        if l.kind in ex:
            continue
        adj.setdefault(l.a, set()).add(l.b)
        adj.setdefault(l.b, set()).add(l.a)
    seen, q = set(roots), deque(roots)
    while q:
        n = q.popleft()
        for m in adj.get(n, ()):
            if m in ok and m not in seen:
                seen.add(m)
                q.append(m)
    return seen


def migration_blocks(topo: Topology, root: str, selected: Iterable[str], target_version: str,
                     protocols: dict[str, str], exclude_kinds: Iterable[str] = ()) -> dict[str, str]:
    """Nodes this upgrade must not touch because of routing, with the reason.

    Judged on the mesh as it will be once every selected node runs ``target_version``.
    Unknown protocols are treated cautiously: not counted as speaking Babel (path check) and
    counted as possibly OLSR-only (stranding check).
    """
    sel = set(selected)
    target = routing_of(target_version)
    if target != BABEL:
        return {}          # moving to OLSR+Babel (or an unknown nightly) can't strand anything
    now = {n: protocols.get(n, UNKNOWN) for n in topo.nodes}
    blocks: dict[str, str] = {}

    for n in sorted(sel):
        if now[n] == OLSR:
            blocks[n] = ("speaks only OLSR: going straight to Babel-only firmware would cut it "
                         "off from its neighbours. Upgrade it to an OLSR + Babel release "
                         "(3.25.5.0 or later 3.25.x) first.")
        elif now[n] == UNKNOWN:
            blocks[n] = ("routing protocol unknown (nightly firmware): check that it runs Babel "
                         "before moving it to Babel-only firmware.")

    # An OLSR-only node is part of the mesh while it can reach an OLSR + Babel node (the bridge
    # to the Babel side) through nodes that speak OLSR.
    olsr_now = {n for n, p in now.items() if speaks_olsr(p) or p == UNKNOWN}
    reach_now = _reach(topo, {n for n, p in now.items() if p == DUAL}, olsr_now, exclude_kinds)

    # A blocked node stays on its current firmware, which can change what the others may do,
    # so repeat until nothing new is blocked.
    while True:
        going = sel - set(blocks)
        after = {n: (BABEL if n in going else p) for n, p in now.items()}
        added = False

        # 1. After the run, each upgraded node must still be reachable over Babel.
        babel_ok = {n for n, p in after.items() if speaks_babel(p)}
        reach_b = _reach(topo, root, babel_ok, exclude_kinds)
        for n in sorted(going):
            if n not in reach_b:
                blocks[n] = ("after this upgrade nothing on its path back to this computer would "
                             "speak Babel, so it would be unreachable. Upgrade the nodes between "
                             "it and this computer to an OLSR + Babel release first.")
                added = True
        if added:
            continue

        # 2. Nodes that stay OLSR-only and are reachable over OLSR now must stay reachable.
        olsr_after = {n for n, p in after.items() if speaks_olsr(p) or p == UNKNOWN}
        bridges_after = {n for n, p in after.items() if p == DUAL}
        reach_after = _reach(topo, bridges_after, olsr_after, exclude_kinds)
        stranded = sorted(n for n in reach_now - reach_after
                          if after[n] in (OLSR, UNKNOWN))
        if stranded:
            blamed: set[str] = set()
            for n in sorted(going):
                # Would they stay reachable if this node kept speaking OLSR?
                keep = {n} if now[n] == DUAL else set()
                rescue = _reach(topo, bridges_after | keep, olsr_after | keep, exclude_kinds)
                lost = [s for s in stranded if s in rescue]
                if lost:
                    blocks[n] = (f"OLSR-only node(s) {', '.join(lost)} reach the mesh through it; "
                                 "once it speaks only Babel they'd be cut off. Upgrade them to an "
                                 "OLSR + Babel release first.")
                    blamed |= set(lost)
                    added = True
            rest = [s for s in stranded if s not in blamed]
            if rest:
                # Several bridges in a row: no single one is to blame, but together they'd cut
                # them off.
                for n in sorted(going):
                    if now[n] == DUAL:
                        blocks[n] = (f"together with the other selected nodes it carries the OLSR "
                                     f"paths of {', '.join(rest)}, which would be cut off. Upgrade "
                                     "those to an OLSR + Babel release first.")
                        added = True
        if not added:
            return blocks
