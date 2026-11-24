"""Radio settings job: per-radio (per-band) mode, channel and width, with an on-node dead-man revert.

What AREDN 4.26 does (read from a bench node running 4.26.1.0):

* Each radio is ``radioN`` in ``/etc/config.mesh/setup`` (uci ``setup.globals``):
  ``radioN_mode`` (``mesh`` | ``meshap`` (PtMP) | ``meshptp`` | ``meshsta``
  (Mesh Station) | ``lan`` | ``wan`` | ``off``), ``radioN_channel``,
  ``radioN_bandwidth``, ``radioN_ssid``, ``radioN_peer``.
* ``aredn.radios.getConfiguration()`` lists every radio: ``iface``, current
  ``mode``, ``def.band`` ("2.4GHz", "5GHz", ...), supported widths (``bws``),
  a channel table per width (``{number, label, frequency}``) and
  ``managedOOB``. It also returns the WAN-client SSID and key (hex); the
  manager drops those on read and never stores them.
* The node's Radios & Antennas dialog builds its channel dropdown from
  ``channels[width]`` using each entry's ``label``, and in Mesh PtMP, Mesh PtP
  and Mesh Station modes it hides channels listed in ``managedOOB``.
  ``channel_options()`` reproduces that exactly, so the fleet manager offers
  the same choices the node's own screen does.
* When a radio's mode changes, the dialog also resets its channel, width and
  SSID (``AREDN`` for mesh modes). On this firmware two radios may not both be
  in a mesh mode (the dialog turns the other one off); the release candidate
  lifts that, so it's probed per node.
* ``aredn.configuration`` ``prepareChanges/setSetting/saveSettings/
  commitChanges`` applies settings exactly as the dialog does. On 4.26.1.0 any
  wireless change needs a reboot (``node-setup``'s ``wireless`` case falls
  through to ``default``).

Every change cuts RF links to neighbours still on the old settings, so all
nodes in an RF cluster switch **together at one scheduled time**, and each one
carries a revert timer that survives the reboot. The controller confirms only
if every node in the cluster is back on the new settings with its RF
neighbours. Otherwise nobody is confirmed, and the whole cluster reverts.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..topology import Topology
from ..transport import NodeSession, detached

MESH_MODES = {"mesh", "meshap", "meshptp", "meshsta"}
MANAGED_MODES = {"meshap", "meshptp", "meshsta"}   # the dialog applies managedOOB to these
SETTABLE_MODES = MESH_MODES | {"off"}
MODE_LABELS = {"mesh": "Mesh", "meshap": "Mesh PtMP", "meshptp": "Mesh PtP",
               "meshsta": "Mesh Station", "lan": "LAN Hotspot", "wan": "WAN Client", "off": "Off"}
PERSIST_DIR = "/etc/aredn-fleet-radio"   # on flash: must survive the reboot
REMOTE_SCRIPT = "/tmp/aredn-fleet-radio.sh"
RC_LOCAL_LINE = f"[ -x {PERSIST_DIR}/boot-check ] && {PERSIST_DIR}/boot-check &"

# getConfiguration() plus, per radio, which purposes its hardware supports, asked the way the
# node's Radios & Antennas dialog asks (hardware.supportsFeature("wifi-mode", iface, mode)).
RADIO_STATE_CMD = (
    "ucode -e 'import * as r from \"aredn.radios\"; import * as h from \"aredn.hardware\"; "
    "const c = r.getConfiguration(); const modes = []; "
    "for (let i = 0; i < length(c); i++) { let m = null; "
    "try { if (h.supportsFeature && c[i]) { m = {}; "
    "for (let x in [\"mesh\", \"meshap\", \"meshptp\", \"meshsta\", \"lan\", \"wan\"]) "
    "m[x] = h.supportsFeature(\"wifi-mode\", c[i].iface, x); } } catch (e) { m = null; } "
    "push(modes, m); } "
    "printf(\"%J\", { config: c, modes: modes });'"
)
# Whether the node shows unlicensed (Part 15) limits next to channels in its dialog.
UNLICENSED_CMD = "uci -q -c /etc/config.mesh get setup.globals.radio_unlicensed"
# The dialog code that forbids two mesh radios, present on firmware that can't do it.
CAPS_CMD = ("f=/app/main/status/e/radio-and-antenna.ut; "
            "if [ ! -f $f ]; then echo unknown; "
            "elif grep -q 'substr(radio0_mode, 0, 4) === substr(radio1_mode, 0, 4)' $f; "
            "then echo single-mesh; "
            # Newer dialogs instead turn the other radio off on hardware flagged only_one_radio.
            "elif ucode -e 'import * as h from \"aredn.hardware\"; "
            "print(h.supportsFeature(\"only_one_radio\") ? \"yes\" : \"no\");' 2>/dev/null | grep -q yes; "
            "then echo single-mesh; else echo multi-mesh; fi")
# The revert timer is restarted from /etc/rc.local, which OpenWrt runs from /etc/init.d/done.
BOOT_HOOK_CHECK = ("[ -f /etc/rc.local ] && ls /etc/rc.d/ 2>/dev/null | grep -q 'done$' "
                   "&& grep -q rc.local /etc/init.d/done && echo ok")

Connector = Callable[[str], Awaitable[NodeSession]]
SysinfoFetch = Callable[[str], Awaitable[dict[str, Any] | None]]


# --------------------------------------------------------------------------- inventory

def band_label(band: str) -> str:
    """"5GHz" -> "5 GHz", "900MHz" -> "900 MHz", "3GHz" -> "3.4 GHz" (the ham 3 GHz allocation)."""
    m = re.fullmatch(r"([\d.]+)\s*(GHz|MHz)", band or "", re.I)
    if not m:
        return band or "unknown band"
    num, unit = m.group(1), "GHz" if m.group(2).lower() == "ghz" else "MHz"
    if unit == "GHz" and num == "3":
        num = "3.4"
    return f"{num} {unit}"


def channel_table(radio: dict[str, Any], unlicensed: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Width -> channel options, from either firmware's ``getConfiguration()`` format.

    * 4.26.x: ``channels`` is ``{width: [{number, label, frequency}]}``.
    * Nightly/RC (from 20261012): ``channels`` is one list of
      ``{number, label, frequency, widths, unlicensed: {width: limits}, amateur: {width: limits}}``.
      The node's dialog lists every channel whose ``widths`` include the width, and labels it
      ``<label> [ham <maxPEP> dBm] [dfs, indoor, <maxEIRP> dB]`` (the unlicensed part only when
      unlicensed channels are enabled). This builds the same labels.
    """
    chans = radio.get("channels") or {}
    if isinstance(chans, dict):
        return {str(bw): [{"number": int(c["number"]), "label": str(c.get("label", c["number"])),
                           "frequency": int(c.get("frequency") or 0)}
                          for c in lst if isinstance(c, dict)]
                for bw, lst in chans.items()}
    table: dict[str, list[dict[str, Any]]] = {}
    widths = [int(b) for b in (radio.get("bws") or [])] or sorted(
        {int(w) for c in chans if isinstance(c, dict) for w in (c.get("widths") or [])})
    for bw in widths:
        opts = []
        for c in chans:
            if not isinstance(c, dict) or bw not in [int(w) for w in (c.get("widths") or [])]:
                continue
            label = str(c.get("label", c["number"]))
            ham = ((c.get("amateur") or {}).get(str(bw)) or {})
            if ham.get("maxPEP"):
                label += f" [ham {ham['maxPEP']} dBm]"
            if unlicensed:
                u = ((c.get("unlicensed") or {}).get(str(bw)) or {})
                limits = (["dfs"] if u.get("DFS") else []) + (["indoor"] if u.get("NO_OUTDOOR") else []) \
                    + ([f"{u['maxEIRP']} dB"] if u.get("maxEIRP") else [])
                if limits:
                    label += f" [{', '.join(limits)}]"
            opts.append({"number": int(c["number"]), "label": label,
                         "frequency": int(c.get("frequency") or 0)})
        table[str(bw)] = opts
    return table


def band_of(radio: dict[str, Any]) -> str:
    """The radio's band as the node reports it ("2.4GHz", "5GHz", "900MHz", ...), else from frequency."""
    b = str(((radio.get("def") or {}).get("band") or "")).replace(" ", "")
    if b:
        return b
    for chans in channel_table(radio).values():
        for c in chans:
            f = c.get("frequency") if isinstance(c, dict) else None
            if f:
                return ("900MHz" if f < 1000 else "2.4GHz" if f < 2500
                        else "3.4GHz" if f < 4000 else "5GHz" if f < 5925 else "6GHz")
    return "unknown"


@dataclass
class RadioInfo:
    index: int
    iface: str
    band: str
    mode: str
    channel: int | None
    bandwidth: int | None
    bws: list[int]
    channels: dict[str, list[dict[str, Any]]]   # width -> [{number, label, frequency}]
    managed_oob: list[int] = field(default_factory=list)
    ssid: str | None = None   # mesh SSID base ("AREDN"); only kept for mesh-mode radios
    # Purposes this radio's hardware supports, as the node reports them; None = the firmware
    # didn't say (older firmware), so nothing is ruled out on that basis.
    modes: list[str] | None = None

    def channel_options(self, mode: str, bw: int | str) -> list[dict[str, Any]]:
        """Exactly the options the node's own Channel dropdown shows for this mode and width."""
        opts = self.channels.get(str(bw), [])
        if mode in MANAGED_MODES and self.managed_oob:
            opts = [c for c in opts if int(c["number"]) not in self.managed_oob]
        return opts

    def frequency(self, ch: int, bw: int | str) -> int | None:
        for c in self.channels.get(str(bw), []):
            if int(c["number"]) == int(ch):
                return int(c.get("frequency") or 0) or None
        return None

    def label(self, ch: int | None, bw: int | str | None) -> str:
        for c in self.channels.get(str(bw), []):
            if ch is not None and int(c["number"]) == int(ch):
                return str(c["label"])
        return str(ch)


@dataclass
class NodeRadios:
    radios: list[RadioInfo]
    multi_mesh: bool | None          # None = couldn't tell
    read_at: float = field(default_factory=time.time)
    firmware: str = ""               # firmware the node ran when read (channel lists can change)

    def by_band(self, band: str) -> RadioInfo | None:
        return next((r for r in self.radios if r.band == band), None)

    def to_json(self) -> dict[str, Any]:
        return {"radios": [asdict(r) for r in self.radios], "multi_mesh": self.multi_mesh,
                "read_at": self.read_at, "firmware": self.firmware}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "NodeRadios":
        known = set(RadioInfo.__dataclass_fields__)
        return cls([RadioInfo(**{k: v for k, v in r.items() if k in known}) for r in d.get("radios", [])],
                   d.get("multi_mesh"),
                   d.get("read_at", 0), d.get("firmware", ""))


def _supported_modes(m: Any) -> list[str] | None:
    """Modes the node says the radio supports. All-false means the firmware didn't answer the
    question (it would never offer no purpose at all), so treat that as unknown."""
    if not isinstance(m, dict):
        return None
    yes = [k for k, v in m.items() if v is True]
    return sorted(yes) if yes else None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_radio_inventory(raw: str, caps: str = "unknown", unlicensed: bool = False) -> NodeRadios:
    """Parse RADIO_STATE_CMD output. Keys and non-mesh SSIDs are deliberately dropped."""
    data = json.loads(raw)
    radios = []
    reported = data.get("modes") or []
    for i, r in enumerate(data.get("config") or []):
        if not isinstance(r, dict):
            continue
        m = r.get("mode") or {}
        chans = channel_table(r, unlicensed)
        oob = r.get("managedOOB") or []
        radios.append(RadioInfo(
            index=i, iface=str(r.get("iface") or f"wlan{i}"), band=band_of(r),
            mode=str(m.get("mode") or "off"),
            channel=_int_or_none(m.get("channel")), bandwidth=_int_or_none(m.get("bandwidth")),
            bws=[int(b) for b in (r.get("bws") or [])], channels=chans,
            managed_oob=[int(x) for x in oob if _int_or_none(x) is not None],
            # The base name of a *mesh* SSID isn't secret; a WAN client's SSID/key are dropped.
            ssid=(str(m.get("ssid")) if m.get("mode") in MESH_MODES and m.get("ssid") else None),
            modes=_supported_modes(reported[i] if i < len(reported) else None)))
    multi = {"multi-mesh": True, "single-mesh": False}.get(caps.strip())
    return NodeRadios(radios, multi)


async def read_node_radios(session: NodeSession) -> NodeRadios:
    raw = (await session.run(RADIO_STATE_CMD)).stdout
    caps = (await session.run(CAPS_CMD)).stdout
    try:
        unl = (await session.run(UNLICENSED_CMD)).stdout.strip() == "1"
    except Exception:  # noqa: BLE001 - only affects labels
        unl = False
    inv = parse_radio_inventory(raw, caps, unl)
    try:
        inv.firmware = (await session.run("cat /etc/mesh-release")).stdout.strip().splitlines()[0]
    except (IndexError, AttributeError):
        pass
    return inv


SSID_RE = re.compile(r"[A-Za-z0-9_.-]{1,20}")
DEFAULT_SSID = "AREDN"


def full_ssid(base: str, mode: str, channel: int | None, width: int | None, band: str) -> str:
    """The SSID node-setup builds: base-<bw>-v3 for Mesh (base-<ch>-<bw>-v3 on 900 MHz HaLow),
    base-<ch>-<bw>-v3 for PtMP / PtP / Station."""
    if mode == "mesh" and not band.startswith("900"):
        return f"{base}-{width}-v3"
    return f"{base}-{channel}-{width}-v3"


# --------------------------------------------------------------------------- requested change

@dataclass(frozen=True)
class BandChange:
    band: str                       # "5GHz", "2.4GHz", ...
    mode: str | None = None         # None = leave as is
    channel: int | None = None
    width: int | None = None
    peer: str | None = None         # required for meshptp
    ssid: str | None = None         # mesh SSID base, e.g. "AREDN" (node appends -<bw>-v3)

    def validate(self) -> None:
        if self.mode is not None and self.mode not in SETTABLE_MODES:
            raise ValueError(f"{self.band}: mode must be one of {sorted(SETTABLE_MODES)}")
        if self.mode == "meshptp" and not self.peer:
            raise ValueError(f"{self.band}: Mesh PtP needs the peer's MAC address")
        if self.peer and not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", self.peer):
            raise ValueError(f"{self.band}: peer must be a MAC address like aa:bb:cc:dd:ee:ff")
        if self.ssid is not None and not SSID_RE.fullmatch(self.ssid):
            raise ValueError(f"{self.band}: SSID must be 1-20 letters, digits, '-', '_' or '.' "
                             f"(the node adds -<width>-v3)")
        if self.mode is None and self.channel is None and self.width is None and self.ssid is None:
            raise ValueError(f"{self.band}: nothing to change")


def merge_changes(group: list[BandChange], custom: dict[str, dict[str, Any]] | None) -> list[BandChange]:
    """The changes one node should get: group changes, except on bands where the node
    has its own settings.

    ``custom`` is ``{band: {mode, channel, width, ssid, peer}}``. On a band with
    own settings, the group's change for that band is ignored completely: the
    node's own values are applied, and any field it leaves blank keeps the
    node's *current* value (it is not switched to the group's). Bands without
    own settings follow the group. Own settings are (re)applied whenever the
    node is part of a radio job; if the node already matches, nothing changes.
    """
    custom = custom or {}
    out: dict[str, BandChange] = {c.band: c for c in group if c.band not in custom}
    for band, vals in custom.items():
        out[band] = BandChange(band, **{k: (vals.get(k) if vals.get(k) not in ("",) else None)
                                       for k in ("mode", "channel", "width", "peer", "ssid")})
    return [c for c in out.values()
            if any(getattr(c, k) is not None for k in ("mode", "channel", "width", "ssid"))]


def validate_changes(changes: list[BandChange]) -> None:
    if not changes:
        raise ValueError("nothing to change")
    bands = [c.band for c in changes]
    if len(set(bands)) != len(bands):
        raise ValueError("one change per band")
    for c in changes:
        c.validate()


@dataclass
class NodePlan:
    """What will be written on one node, after checking it against that node's radios."""
    sets: list[tuple[int, str, str]] = field(default_factory=list)   # (radio index, key, value)
    expect: list[dict[str, Any]] = field(default_factory=list)        # live checks after switch
    notes: list[str] = field(default_factory=list)
    final: dict[int, tuple] = field(default_factory=dict)             # radio index -> (mode, ch, bw, ssid)


def plan_node(inv: NodeRadios, changes: list[BandChange]) -> NodePlan:
    """Turn band changes into radioN_* settings for this node, or raise ValueError."""
    plan = NodePlan()
    final_modes = {r.index: r.mode for r in inv.radios}
    for ch in changes:
        same = [x for x in inv.radios if x.band == ch.band]
        if len(same) > 1:
            raise ValueError(f"this node has {len(same)} radios on {band_label(ch.band)}; "
                             "the fleet manager can't tell which one to change yet")
        r = inv.by_band(ch.band)
        if r is None:
            plan.notes.append(f"no {band_label(ch.band)} radio; skipped")
            continue
        mode = ch.mode or r.mode
        if mode not in MESH_MODES and mode != "off":
            raise ValueError(f"{ch.band} radio is a {MODE_LABELS.get(mode, mode)}; the fleet "
                             f"manager only changes mesh radios (or turns them off)")
        if ch.mode and ch.mode != "off" and r.modes is not None and ch.mode not in r.modes:
            raise ValueError(f"{band_label(ch.band)}: this radio doesn't support "
                             f"{MODE_LABELS.get(ch.mode, ch.mode)} (the node offers "
                             f"{', '.join(MODE_LABELS.get(x, x) for x in r.modes)})")
        mode_changed = ch.mode is not None and ch.mode != r.mode
        width = ch.width if ch.width is not None else r.bandwidth
        channel = ch.channel if ch.channel is not None else r.channel
        before = len(plan.sets)
        if mode in MESH_MODES:
            if width is None or width not in r.bws:
                raise ValueError(f"{ch.band}: {width} MHz isn't offered on this radio "
                                 f"({', '.join(map(str, r.bws))} MHz)")
            allowed = [int(c["number"]) for c in r.channel_options(mode, width)]
            if channel not in allowed:
                raise ValueError(f"{ch.band}: channel {channel} isn't offered at {width} MHz in "
                                 f"{MODE_LABELS[mode]} mode on this radio")
            if mode_changed:
                plan.sets.append((r.index, "mode", mode))
            # SSID base. Requested -> set it if different. A radio entering a mesh mode gets
            # AREDN, as the dialog does (never a former WAN client's network name). Otherwise
            # the node's current SSID is left alone, even if we couldn't read it, so a group
            # on a non-standard SSID is never moved back to AREDN by accident.
            if ch.ssid is not None:
                ssid = ch.ssid
                if ssid != r.ssid:
                    plan.sets.append((r.index, "ssid", ssid))
            elif r.mode not in MESH_MODES:
                ssid = DEFAULT_SSID
                plan.sets.append((r.index, "ssid", ssid))
            else:
                ssid = r.ssid
            if mode_changed or channel != r.channel:
                plan.sets.append((r.index, "channel", str(int(channel))))
            if mode_changed or width != r.bandwidth:
                plan.sets.append((r.index, "bandwidth", str(int(width))))
            if mode == "meshptp" and ch.peer:
                plan.sets.append((r.index, "peer", ch.peer.lower()))
            if len(plan.sets) > before:
                plan.expect.append({"iface": r.iface, "band": ch.band, "mode": mode,
                                    "frequency": r.frequency(channel, width), "width": width,
                                    "ssid": full_ssid(ssid, mode, channel, width, ch.band) if ssid else None})
            plan.final[r.index] = (mode, channel, width, ssid)
        elif mode == "off" and mode_changed:
            plan.sets.append((r.index, "mode", "off"))
            plan.expect.append({"iface": r.iface, "band": ch.band, "mode": "off"})
            plan.final[r.index] = ("off", None, None, None)
        final_modes[r.index] = mode
        if len(plan.sets) == before:
            plan.notes.append(f"{ch.band} already set that way")
    if sum(1 for m in final_modes.values() if m in MESH_MODES) > 1 and inv.multi_mesh is not True:
        raise ValueError("this firmware can't run mesh on two radios at once "
                         "(AREDN's release candidate / nightly can)")
    return plan


LINK_CLASS = {"mesh": "adhoc", "meshap": "ptmp", "meshsta": "ptmp", "meshptp": "ptp"}


def link_signatures(inv: NodeRadios, plan: NodePlan | None = None) -> set[tuple]:
    """What each mesh radio will look like on air: two nodes can link over RF only if
    they share a signature (band, link type, channel, width, full SSID)."""
    sigs = set()
    for r in inv.radios:
        mode, ch, bw, ssid = (plan.final.get(r.index) if plan and r.index in plan.final
                              else (r.mode, r.channel, r.bandwidth, r.ssid or DEFAULT_SSID))
        if mode in MESH_MODES:
            sigs.add((r.band, LINK_CLASS[mode], ch, bw, full_ssid(ssid, mode, ch, bw, r.band)))
    return sigs


# --------------------------------------------------------------------------- clusters

def rf_clusters(topo: Topology, nodes: set[str]) -> list[set[str]]:
    """Group nodes into components joined by RF links (the ones a change breaks)."""
    adj: dict[str, set[str]] = {n: set() for n in topo.nodes}
    for l in topo.links:
        if l.kind == "RF":
            adj[l.a].add(l.b)
            adj[l.b].add(l.a)
    seen: set[str] = set()
    out = []
    for n in sorted(nodes):
        if n in seen:
            continue
        comp, stack = set(), [n]
        while stack:
            c = stack.pop()
            if c in comp:
                continue
            comp.add(c)
            stack.extend(adj.get(c, ()))
        seen |= comp
        out.append(comp)
    return out


def cluster_warnings(topo: Topology, selected: set[str]) -> list[str]:
    warns = []
    for comp in rf_clusters(topo, selected):
        left_out = comp - selected
        if left_out:
            warns.append("RF neighbours not selected will lose their links to the changed nodes: "
                          + ", ".join(sorted(left_out)))
    return warns


# --------------------------------------------------------------------------- on-node script

def build_apply_script(sets: list[tuple[int, str, str]], apply_at: int, revert_after: int) -> str:
    """Shell script run on the node.

    On 4.26.1.0 any wireless change needs a reboot, which wipes /tmp, so the
    revert timer lives on flash in ``/etc/aredn-fleet-radio`` and is restarted
    from ``/etc/rc.local`` (in the sysupgrade keep list) at boot. It counts from
    boot, not wall-clock time, so it doesn't depend on NTP being up yet.
    """
    for _, k, v in sets:  # values are whitelisted or numeric; keep it that way
        if not re.fullmatch(r"[A-Za-z0-9:._-]+", v) or not re.fullmatch(r"[a-z_]+", k):
            raise ValueError(f"refusing unsafe setting {k}={v!r}")
    # 4.26.x has setSetting(); the nightly/RC renamed it setSettingAsString() and added list
    # settings. Setting a channel clears radioN_channels (multi-channel Mesh Station), as the
    # node's own dialog does.
    parts = []
    for i, k, v in sets:
        parts.append(f'S("radio{i}_{k}", "{v}");')
        if k == "channel":
            parts.append(f'if (c.setSettingAsList) c.setSettingAsList("radio{i}_channels", []);')
    ucode = ("import * as c from \"aredn.configuration\"; "
             "const S = c.setSettingAsString || c.setSetting; c.prepareChanges(); "
             f"{' '.join(parts)} c.saveSettings(); c.commitChanges();")
    P = PERSIST_DIR
    return f"""#!/bin/sh
# Generated by aredn-fleet. Applies radio settings at a set time; reverts them
# automatically unless the controller confirms within {revert_after}s, even
# across the reboot the change needs.
P={P}
mkdir -p $P
rm -f $P/confirmed $P/cancel
cp /etc/config.mesh/setup $P/setup.orig || {{ echo backup-failed > $P/state; exit 1; }}

cat > $P/boot-check <<'BOOTCHECK'
#!/bin/sh
P={P}
[ -d $P ] || exit 0
sleep {revert_after}
[ -d $P ] || exit 0
if [ -f $P/confirmed ] || [ -f $P/cancel ]; then
  rm -rf $P
  sed -i '/aredn-fleet-radio/d' /etc/rc.local
  exit 0
fi
cp $P/setup.orig /etc/config.mesh/setup
rm -rf $P
sed -i '/aredn-fleet-radio/d' /etc/rc.local
logger -t aredn-fleet "radio change not confirmed; reverted"
/usr/local/bin/node-setup > /tmp/aredn-fleet-revert.log 2>&1
if [ -f /tmp/reboot-required/reboot ]; then reboot; else /usr/local/bin/restart-services; fi
BOOTCHECK
chmod +x $P/boot-check

if ! grep -q aredn-fleet-radio /etc/rc.local; then
  if grep -q '^exit 0' /etc/rc.local; then
    sed -i '/^exit 0/i {RC_LOCAL_LINE}' /etc/rc.local
  else
    echo '{RC_LOCAL_LINE}' >> /etc/rc.local
  fi
fi
echo armed > $P/state

now=$(date +%s)
[ {apply_at} -gt "$now" ] && sleep $(({apply_at} - now))
if [ -f $P/cancel ]; then
  rm -rf $P; sed -i '/aredn-fleet-radio/d' /etc/rc.local; exit 0
fi
ucode -e {shlex.quote(ucode)} > $P/apply.log 2>&1
echo applied > $P/state
if [ -f /tmp/reboot-required/reboot ]; then
  echo rebooting >> $P/state
  sync
  reboot
else
  $P/boot-check &
fi
"""


def cleanup_command() -> str:
    """Remove all traces of the revert machinery (after confirm or cancel)."""
    return f"rm -rf {PERSIST_DIR}; sed -i '/aredn-fleet-radio/d' /etc/rc.local"


# --------------------------------------------------------------------------- live checks

IW_TYPE = {"mesh": "IBSS", "meshap": "AP", "meshsta": "managed"}


def iw_state(output: str) -> dict[str, Any]:
    s: dict[str, Any] = {}
    if m := re.search(r"type (\S+)", output):
        s["type"] = m.group(1)
    if m := re.search(r"\((\d+) MHz\)", output):
        s["frequency"] = int(m.group(1))
    if m := re.search(r"width: (\d+) MHz", output):
        s["width"] = int(m.group(1))
    if m := re.search(r"^\s*ssid (.+)$", output, re.M):
        s["ssid"] = m.group(1).strip()
    return s


def expectation_met(exp: dict[str, Any], iw_out: str) -> bool:
    s = iw_state(iw_out)
    if exp["mode"] == "off":
        return "type" not in s                       # interface gone
    if exp.get("frequency") and s.get("frequency") != exp["frequency"]:
        return False
    want = IW_TYPE.get(exp["mode"])
    if want and s.get("type") != want:
        return False
    # A station may not be associated yet (no ssid line); otherwise the SSID must match.
    if exp.get("ssid") and s.get("ssid") not in (None, exp["ssid"]):
        return False
    # 5/10 MHz are a driver setting that iw may report as 20 MHz; only check >= 20.
    if exp.get("width", 0) >= 20 and s.get("width") not in (None, exp["width"]):
        return False
    return True


def rf_neighbours(si: dict[str, Any] | None) -> set[str]:
    out = set()
    for e in ((si or {}).get("link_info") or {}).values():
        if e.get("hostname") and str(e.get("linkType", "")).upper() == "RF":
            out.add(e["hostname"].lower().removesuffix(".local.mesh"))
    return out


# --------------------------------------------------------------------------- job

@dataclass
class RadioConfig:
    lead_seconds: float = 90        # time from arming the last node to the switch
    revert_after: int = 600         # node reverts unless confirmed within this
    poll_interval: float = 15
    max_clock_skew: float = 20


@dataclass
class RadioNodeResult:
    node: str
    status: str = "pending"   # would-change, unchanged, armed, verified, confirmed, failed
    detail: str = ""
    before: str = ""
    ledger: list[dict[str, Any]] = field(default_factory=list)   # per radio: before -> after
    lost_links: list[str] = field(default_factory=list)          # RF links dropped on purpose


@dataclass
class RadioReport:
    results: dict[str, RadioNodeResult]
    confirmed: bool
    reason: str = ""


def _radio_view(r: RadioInfo, mode: str, ch: int | None, bw: int | None, ssid: str | None) -> dict:
    mesh = mode in MESH_MODES
    return {"mode": MODE_LABELS.get(mode, mode),
            "channel": r.label(ch, bw) if mesh else None,
            "width": f"{bw} MHz" if mesh and bw else None,
            "ssid": full_ssid(ssid or DEFAULT_SSID, mode, ch, bw, r.band) if mesh else None}


def ledger_for(inv: NodeRadios, plan: NodePlan) -> list[dict[str, Any]]:
    """Per radio the plan touches: what it is now and what it will be, for review screens."""
    rows = []
    for idx, (mode, ch, bw, ssid) in sorted(plan.final.items()):
        r = next(x for x in inv.radios if x.index == idx)
        before = _radio_view(r, r.mode, r.channel, r.bandwidth, r.ssid)
        after = _radio_view(r, mode, ch, bw, ssid)
        rows.append({"band": r.band, "band_label": band_label(r.band), "before": before,
                     "after": after, "changed": before != after})
    return rows


def describe(inv: NodeRadios) -> str:
    parts = []
    for r in inv.radios:
        if r.mode in MESH_MODES:
            parts.append(f"{band_label(r.band)} {MODE_LABELS[r.mode]} ch "
                         f"{r.label(r.channel, r.bandwidth)} @ {r.bandwidth} MHz")
        else:
            parts.append(f"{band_label(r.band)} {MODE_LABELS.get(r.mode, r.mode)}")
    return "; ".join(parts)


async def run_radio_change(cluster: list[str],
                           changes: list[BandChange] | dict[str, list[BandChange]],
                           connect: Connector, sysinfo: SysinfoFetch,
                           cfg: RadioConfig | None = None, execute: bool = False,
                           sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                           now: Callable[[], float] = time.time,
                           log: Callable[..., None] = lambda *a, **k: None,
                           on_inventory: Callable[[str, NodeRadios], None] | None = None
                           ) -> RadioReport:
    """Change one RF cluster together. Dry run (default) does only the read-only preflight.

    ``changes`` is either one list for every node, or a per-node dict (group
    settings already merged with each node's own settings; see ``merge_changes``).
    """
    cfg = cfg or RadioConfig()
    per_node = changes if isinstance(changes, dict) else {n: list(changes) for n in cluster}
    if not any(per_node.get(n) for n in cluster):
        raise ValueError("nothing to change")
    for n in cluster:
        if per_node.get(n):
            validate_changes(per_node[n])
    results = {n: RadioNodeResult(n) for n in cluster}
    plans: dict[str, NodePlan] = {}
    invs: dict[str, NodeRadios] = {}

    # ---- preflight (read-only)
    for n in cluster:
        r = results[n]
        try:
            s = await connect(n)
            try:
                inv = await read_node_radios(s)
                node_time = float((await s.run("date +%s")).stdout.strip() or 0)
                hook = (await s.run(BOOT_HOOK_CHECK)).stdout.strip()
            finally:
                await s.close()
        except Exception as exc:  # noqa: BLE001
            r.status, r.detail = "failed", f"preflight: {exc}"
            continue
        invs[n] = inv
        if on_inventory:
            on_inventory(n, inv)
        r.before = describe(inv)
        if not per_node.get(n):
            r.status, r.detail = "unchanged", "no settings to apply"
            continue
        try:
            plan = plan_node(inv, per_node[n])
        except ValueError as exc:
            r.status, r.detail = "failed", str(exc)
            continue
        if not plan.sets:
            r.status, r.detail = "unchanged", "; ".join(plan.notes) or "already set that way"
            continue
        if abs(node_time - now()) > cfg.max_clock_skew:
            r.status, r.detail = "failed", f"clock off by {node_time - now():+.0f}s; fix NTP first"
            continue
        if hook != "ok":
            r.status, r.detail = "failed", ("node doesn't run /etc/rc.local at boot; the revert "
                                            "timer couldn't survive a reboot")
            continue
        plans[n] = plan
        r.ledger = ledger_for(inv, plan)
        r.status = "would-change"
        r.detail = ", ".join(f"radio{i}_{k}={v}" for i, k, v in plan.sets)
        if plan.notes:
            r.detail += " (" + "; ".join(plan.notes) + ")"

    failed = [n for n, r in results.items() if r.status == "failed"]
    if failed:
        return RadioReport(results, False, f"preflight failed on {', '.join(failed)}; nothing changed")
    if not plans:
        return RadioReport(results, False, "nothing to change on these nodes")

    # Which RF links should exist afterwards? Two nodes keep a link only if they'll
    # share a radio signature. Links a node's own settings deliberately drop are
    # reported here and not waited for later.
    sigs = {n: link_signatures(invs[n], plans.get(n)) for n in invs}
    neighbours_before = {n: rf_neighbours(await sysinfo(n)) & set(cluster) for n in cluster}
    expected: dict[str, set[str]] = {}
    for n in plans:
        keep = {m for m in neighbours_before[n] if m in sigs and sigs[n] & sigs[m]}
        expected[n] = keep
        dropped = neighbours_before[n] - keep
        results[n].lost_links = sorted(dropped)
        if dropped:
            results[n].detail += f"; WILL LOSE RF LINK TO {', '.join(sorted(dropped))} (different settings)"

    if not execute:
        return RadioReport(results, False, "dry run")

    targets = sorted(plans)

    # ---- arm every node with the same apply time
    apply_at = int(now() + cfg.lead_seconds)
    log("-", "radio-arm", apply_at=apply_at, nodes=targets,
        changes={n: [asdict(c) for c in per_node[n]] for n in targets},
        settings={n: plans[n].sets for n in targets})
    armed: list[str] = []
    for n in targets:
        script = build_apply_script(plans[n].sets, apply_at, cfg.revert_after)
        try:
            s = await connect(n)
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
                    f.write(script)
                await s.upload(Path(f.name), REMOTE_SCRIPT)
                Path(f.name).unlink(missing_ok=True)
                await s.run(detached(f"sh {REMOTE_SCRIPT}"), timeout=20)
            finally:
                await s.close()
            armed.append(n)
            results[n].status = "armed"
        except Exception as exc:  # noqa: BLE001
            results[n].status, results[n].detail = "failed", f"arm: {exc}"
            break
    if len(armed) != len(targets):
        for n in armed:  # cancel before the switch time; the scripts exit untouched
            try:
                s = await connect(n)
                await s.run(f"touch {PERSIST_DIR}/cancel; {cleanup_command()}")
                await s.close()
                results[n].status, results[n].detail = "unchanged", "cancelled before switch"
            except Exception as exc:  # noqa: BLE001
                results[n].detail = f"CANCEL FAILED, will switch at {apply_at}: {exc}"
        return RadioReport(results, False, "arming failed; switch cancelled")

    # ---- wait for the switch (and reboot), then verify every node live
    await sleep(max(0.0, apply_at - now()) + 30)
    deadline = apply_at + cfg.revert_after - 90   # leave time to confirm before revert
    pending = set(targets)
    why: dict[str, str] = {}
    while pending and now() < deadline:
        for n in sorted(pending):
            try:
                s = await connect(n)
                try:
                    ok = True
                    for exp in plans[n].expect:
                        out = (await s.run(f"iw dev {shlex.quote(exp['iface'])} info 2>&1",
                                           timeout=15)).stdout
                        if not expectation_met(exp, out):
                            ok, why[n] = False, f"{exp['band']} not on the new settings yet"
                finally:
                    await s.close()
            except Exception as exc:  # noqa: BLE001 - still rebooting
                why[n] = f"not reachable yet ({type(exc).__name__})"
                continue
            if ok:
                missing = expected[n] - rf_neighbours(await sysinfo(n))
                if missing:
                    why[n] = f"RF neighbours not back: {', '.join(sorted(missing))}"
                    continue
                pending.discard(n)
                results[n].status = "verified"
        if pending:
            await sleep(cfg.poll_interval)

    if pending:
        for n in pending:
            results[n].status, results[n].detail = "failed", why.get(n, "not verified")
        reason = (f"not verified: {', '.join(sorted(pending))}. Confirming nobody; the whole "
                  f"cluster reverts about {cfg.revert_after}s after the switch.")
        log("-", "radio-no-confirm", reason=reason)
        return RadioReport(results, False, reason)

    # ---- confirm everyone
    for n in targets:
        try:
            s = await connect(n)
            await s.run(f"touch {PERSIST_DIR}/confirmed; {cleanup_command()}")
            await s.close()
            results[n].status = "confirmed"
        except Exception as exc:  # noqa: BLE001
            results[n].status, results[n].detail = "failed", f"confirm: {exc} (this node will revert)"
    ok = all(results[n].status == "confirmed" for n in targets)
    log("-", "radio-done", confirmed=ok)
    return RadioReport(results, ok, "" if ok else "some nodes could not be confirmed and will revert")
