"""Rules from AREDN's release-candidate notes ("Release Candidate – Major changes and tests").

* The RC process started with nightly 20261012-e3528941; every later nightly
  is an RC build, and the stable release that comes out of it will be 4.26.X.0
  (X > 10).
* A Mesh Station must not be upgraded to the RC until its PtMP access point
  runs 4.26.7.0 or the RC.
* 802.11ac radios lose channels 48, 52, 56 and 64, and any wider channel that
  overlaps them (e.g. 60 at 40 MHz).
* Some MikroTik IPQ40xx units on 4.26.7.0 never finish a reboot; the reboot
  into the RC still needs one power cycle.
"""

from __future__ import annotations

import re
from typing import Any

from .jobs.radio import MESH_MODES, NodeRadios, RadioInfo

NIGHTLY_RE = re.compile(r"^(\d{8})-[0-9a-f]+$")
RC_FIRST_NIGHTLY = "20261012"
REMOVED_AC_CHANNELS = {48: 5240, 52: 5260, 56: 5280, 64: 5320}   # channel -> centre MHz


def _stable(v: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return None


def is_rc_or_later(version: str) -> bool:
    """True for RC nightlies (from 20261012) and stable releases after 4.26.10.0."""
    if m := NIGHTLY_RE.match(version or ""):
        return m.group(1) >= RC_FIRST_NIGHTLY
    s = _stable(version or "")
    return bool(s) and s > (4, 26, 10, 0)


def at_least_4267(version: str) -> bool:
    """Firmware new enough to serve RC Mesh Stations (4.26.7.0 or later, or an RC nightly)."""
    if NIGHTLY_RE.match(version or ""):
        return is_rc_or_later(version)
    s = _stable(version or "")
    return bool(s) and s >= (4, 26, 7, 0)


def is_ac_radio(r: RadioInfo) -> bool:
    # 802.11ac radios offer 80 MHz; 802.11n ones top out at 40.
    return r.band == "5GHz" and any(bw >= 80 for bw in r.bws)


def removed_channel_conflict(r: RadioInfo) -> str | None:
    """Why this radio's current channel won't exist on the RC, or None."""
    if not is_ac_radio(r) or r.mode not in MESH_MODES or r.channel is None or not r.bandwidth:
        return None
    centre = r.frequency(r.channel, r.bandwidth)
    if not centre:
        return None
    half = r.bandwidth / 2
    hits = [ch for ch, c in REMOVED_AC_CHANNELS.items() if abs(centre - c) < half + 10]
    if not hits:
        return None
    return (f"5 GHz radio is on {r.label(r.channel, r.bandwidth)} at {r.bandwidth} MHz, which uses "
            f"channel {', '.join(map(str, sorted(hits)))}; the RC removes channels 48, 52, 56 and 64 "
            f"from AC radios. Move it to another channel before upgrading.")


def station_radios(inv: NodeRadios) -> list[RadioInfo]:
    return [r for r in inv.radios if r.mode == "meshsta"]


def ptmp_pairs(links: list[Any], radios: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """(access point, station) pairs: RF neighbours where one has a Mesh PtMP radio and the
    other a Mesh Station radio on the same band. Needs both nodes' radio details."""
    inv = {n: NodeRadios.from_json(d) for n, d in radios.items()}
    pairs = set()
    for l in links:
        if getattr(l, "kind", "RF") != "RF":
            continue
        for ap, sta in ((l.a, l.b), (l.b, l.a)):
            if ap in inv and sta in inv:
                ap_bands = {r.band for r in inv[ap].radios if r.mode == "meshap"}
                sta_bands = {r.band for r in inv[sta].radios if r.mode == "meshsta"}
                if ap_bands & sta_bands:
                    pairs.add((ap, sta))
    return sorted(pairs)


def ipq40xx_freeze_note(target: str, from_version: str) -> str | None:
    if (target or "").startswith("ipq40xx") and from_version == "4.26.7.0":
        return ("IPQ40xx on 4.26.7.0: some of these units never finish a reboot. This upgrade "
                "may need one power cycle to complete, so have someone on site or remote power "
                "control ready.")
    return None
