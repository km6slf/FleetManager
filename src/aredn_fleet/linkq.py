"""How a node's links look from its neighbours, as their own pages show it.

When a node doesn't answer, the useful question is whether the radio link to it is the problem.
Each AREDN node tracks its links in ``/tmp/lqm.info`` and its status page includes that with
``lqm=1``. Its Local/Neighborhood Nodes table shows, per link: errors (``100 - quality``),
SNR, the neighbour's SNR (``rev_snr``) and round-trip time. This reads the same tracker entries.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

SUFFIX = ".local.mesh"


def _short(name: str) -> str:
    n = (name or "").strip().lower()
    return n[: -len(SUFFIX)] if n.endswith(SUFFIX) else n


def trackers(sysinfo: dict[str, Any]) -> list[dict[str, Any]]:
    """Link trackers from a status page fetched with ``lqm=1`` (both 3.25 and 4.x put them at
    ``lqm.info.trackers``, keyed by MAC address)."""
    t = (((sysinfo or {}).get("lqm") or {}).get("info") or {}).get("trackers") or {}
    if isinstance(t, dict):
        return [v for v in t.values() if isinstance(v, dict)]
    return [v for v in t if isinstance(v, dict)] if isinstance(t, list) else []


def describe_tracker(t: dict[str, Any]) -> str:
    bits = []
    q = t.get("quality")
    if isinstance(q, (int, float)):
        bits.append(f"{100 - q:.0f}% errors")
    if t.get("snr") not in (None, ""):
        bits.append(f"SNR {t['snr']}")
    if t.get("rev_snr") not in (None, ""):
        bits.append(f"their SNR {t['rev_snr']}")
    if isinstance(t.get("rtt"), (int, float)):
        bits.append(f"{t['rtt']:.0f} ms round trip")
    kind = str(t.get("type") or "").upper()
    return (f"{kind} link: " if kind else "") + (", ".join(bits) or "no measurements yet")


def link_report(node: str, neighbours: Iterable[str],
                fetch: Callable[[str], dict[str, Any] | None]) -> list[str]:
    """One line per neighbour that could be asked, saying how it sees its link to ``node``."""
    want = _short(node)
    lines = []
    for nb in sorted(set(neighbours)):
        try:
            si = fetch(nb)
        except Exception:  # noqa: BLE001 - a neighbour that can't be asked just isn't listed
            si = None
        if not si:
            continue
        match = [t for t in trackers(si) if _short(str(t.get("hostname") or "")) == want]
        if match:
            for t in match:
                lines.append(f"{nb} sees its link to {node}: {describe_tracker(t)}")
        else:
            lines.append(f"{nb} doesn't list {node} as a link right now (off, rebooting, or out "
                         "of range)")
    return lines
