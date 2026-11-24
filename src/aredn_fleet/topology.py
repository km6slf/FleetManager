"""Mesh topology model and edge-inward rollout planner.

The planner answers one question: in what order can a set of selected nodes be
taken down (rebooted for a firmware upgrade) so that the manager never cuts
itself off from a node it still has to work on?

Approach
--------
* The mesh is an undirected graph. The manager reaches it through a ``root``
  node (normally the node the manager's host is plugged into, ``localnode``).
* For every node ``v`` we compute ``dependents(v)``: the nodes that become
  unreachable from the root while ``v`` is down. In a tree that is the whole
  subtree; in a mesh with redundant paths it is often empty.
* A selected node is *ready* once all of its selected dependents are finished.
  That gives the outermost-first, one-hop-inward behaviour, but a node that
  only *looks* inner (it has a backup path around it) is not held back.
* Ready nodes are grouped into waves. A wave may hold several nodes only if
  taking them all down at once still leaves every other node reachable (two
  nodes can jointly cut a third even when neither does alone). Within the
  ready set, farther nodes go first.
* The root is always last.

The graph sizes here (tens to a few hundred nodes) make the simple
O(V * (V + E)) approach entirely adequate and easy to audit.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class Link:
    a: str
    b: str
    kind: str = "RF"  # RF, DTD, TUN (tunnel), XLINK, ...


@dataclass
class Topology:
    nodes: set[str] = field(default_factory=set)
    links: list[Link] = field(default_factory=list)

    def add_link(self, a: str, b: str, kind: str = "RF") -> None:
        if a == b:
            return
        self.nodes.update((a, b))
        self.links.append(Link(a, b, kind))

    def adjacency(self, exclude_kinds: Iterable[str] = ()) -> dict[str, set[str]]:
        skip = set(exclude_kinds)
        adj: dict[str, set[str]] = {n: set() for n in self.nodes}
        for link in self.links:
            if link.kind in skip:
                continue
            adj[link.a].add(link.b)
            adj[link.b].add(link.a)
        return adj


def _reachable(adj: dict[str, set[str]], root: str, removed: set[str]) -> set[str]:
    if root in removed or root not in adj:
        return set()
    seen = {root}
    queue = deque([root])
    while queue:
        cur = queue.popleft()
        for nxt in adj[cur]:
            if nxt not in seen and nxt not in removed:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def hop_distances(adj: dict[str, set[str]], root: str) -> dict[str, int]:
    dist = {root: 0}
    queue = deque([root])
    while queue:
        cur = queue.popleft()
        for nxt in adj[cur]:
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                queue.append(nxt)
    return dist


def dependents(adj: dict[str, set[str]], root: str) -> dict[str, set[str]]:
    """For each reachable node v, the nodes cut off from root while v is down."""
    full = _reachable(adj, root, set())
    deps: dict[str, set[str]] = {}
    for v in full:
        if v == root:
            deps[v] = full - {v}
        else:
            deps[v] = full - _reachable(adj, root, {v}) - {v}
    return deps


class PlanError(ValueError):
    pass


@dataclass
class Plan:
    root: str
    waves: list[list[str]]
    distances: dict[str, int]
    dependents: dict[str, set[str]]
    unreachable: set[str]
    warnings: list[str]

    def describe(self) -> str:
        lines = [f"Root (manager attachment point): {self.root}"]
        for i, wave in enumerate(self.waves, 1):
            parts = []
            for n in wave:
                d = self.distances.get(n, "?")
                cut = len(self.dependents.get(n, ()))
                parts.append(f"{n} (hop {d}, cuts off {cut})")
            lines.append(f"Wave {i}: " + ", ".join(parts))
        if self.unreachable:
            lines.append("Skipped, not reachable: " + ", ".join(sorted(self.unreachable)))
        for w in self.warnings:
            lines.append(f"WARNING: {w}")
        return "\n".join(lines)


def plan_rollout(
    topo: Topology,
    root: str,
    selected: Iterable[str],
    max_parallel: int = 1,
    exclude_link_kinds: Iterable[str] = (),
) -> Plan:
    """Order ``selected`` nodes into waves, outermost first, root last.

    ``exclude_link_kinds`` lets the caller ignore links that should not be
    trusted to carry the manager's traffic during a rollout, e.g. ``("TUN",)``
    if internet tunnels are flaky.
    """
    if max_parallel < 1:
        raise PlanError("max_parallel must be >= 1")
    adj = topo.adjacency(exclude_link_kinds)
    if root not in adj:
        raise PlanError(f"root node {root!r} is not in the topology")

    dist = hop_distances(adj, root)
    deps = dependents(adj, root)
    selected_set = set(selected)
    unreachable = {n for n in selected_set if n not in dist}
    pending = selected_set - unreachable
    warnings: list[str] = []

    for n in sorted(pending):
        unselected_cut = deps[n] - selected_set
        if unselected_cut:
            warnings.append(
                f"{n} is the only path to unselected node(s) "
                f"{', '.join(sorted(unselected_cut))}; they will drop off briefly during its reboot"
            )

    done: set[str] = set()
    waves: list[list[str]] = []
    while pending:
        ready = [
            n for n in pending
            if n != root or pending == {root}
            if not ((deps[n] & selected_set) - done - {n})
        ]
        if not ready:  # cannot happen for a connected graph, but never loop forever
            raise PlanError(f"no node is ready; stuck with {sorted(pending)}")
        ready.sort(key=lambda n: (-dist[n], n))

        wave: list[str] = []
        for cand in ready:
            if len(wave) >= max_parallel:
                break
            trial = set(wave) | {cand}
            # Taking the wave down together must not cut off any other node
            # beyond what each member already cuts off on its own and is accounted for.
            still = _reachable(adj, root, trial)
            others = set(dist) - trial
            lost = others - still
            allowed = set().union(*(deps[m] for m in trial))
            if lost - allowed:
                continue
            # Every member must stay reachable while the rest of the wave is
            # down, so it can be verified as soon as it comes back.
            if any(m not in _reachable(adj, root, trial - {m}) for m in trial):
                continue
            # Don't put a node in the same wave as something it depends on.
            if any(cand in deps[m] or m in deps[cand] for m in wave):
                continue
            wave.append(cand)
        if not wave:
            wave = [ready[0]]
        waves.append(wave)
        done.update(wave)
        pending.difference_update(wave)

    return Plan(root, waves, dist, deps, unreachable, warnings)
