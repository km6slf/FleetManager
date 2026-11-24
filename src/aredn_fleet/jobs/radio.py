"""Radio settings job: mesh channel, channel width and mode, with an on-node dead-man revert.

Read on a 4.26.1.0 node:

* Radio settings live in ``/etc/config.mesh/setup`` (uci ``setup.globals``) as
  ``radioN_mode`` (``mesh`` | ``meshap`` (PtMP AP) | ``meshptp`` | ``meshsta``
  | ``lan`` | ``wan`` | ``off``), ``radioN_channel``, ``radioN_bandwidth``
  and ``radioN_peer`` (meshptp).
* The web UI changes them through the ucode module ``aredn.configuration``:
  ``prepareChanges()`` -> ``setSetting(k, v)`` -> ``saveSettings()`` ->
  ``commitChanges()``. The last step runs ``node-setup`` and
  ``restart-services`` and flags ``/tmp/reboot-required`` if a reboot is
  needed. The job uses the same path so the node does exactly what the UI does.
* ``node-setup`` builds the mesh SSID from the width (``AREDN-20-v3``), and
  for PtMP/PtP also from the channel. Nodes on different widths can't link at all.

Every radio change cuts the RF links to neighbours still on the old settings,
so nodes in an RF cluster are switched **together at one scheduled time**, and
each node carries its own revert timer:

1. preflight (read-only): read the radio state and the hardware channel table,
   check the requested channel exists at that width, and check the node clock
   against the controller.
2. arm: upload a small shell script that backs up ``setup``, sleeps until the
   apply time, applies via ``aredn.configuration``, then sleeps
   ``revert_after`` seconds and restores the backup unless
   ``/tmp/aredn-fleet-radio/confirmed`` exists.
3. verify: after the apply time, poll every node's sysinfo until ``meshrf``
   shows the new channel and width.
4. confirm only if **every** node in the cluster verified. Otherwise confirm
   nobody, and the whole cluster reverts together instead of splitting.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..topology import Topology
from ..transport import NodeSession

MESH_MODES = {"mesh", "meshap", "meshptp", "meshsta"}
STATE_DIR = "/tmp/aredn-fleet-radio"
REMOTE_SCRIPT = "/tmp/aredn-fleet-radio.sh"

RADIO_STATE_CMD = (
    "ucode -e 'import * as r from \"aredn.radios\"; "
    "printf(\"%J\", { config: r.getConfiguration(), common: r.getCommonConfiguration() });'"
)

Connector = Callable[[str], Awaitable[NodeSession]]
SysinfoFetch = Callable[[str], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class RadioChange:
    channel: int | None = None
    bandwidth: int | None = None
    mode: str | None = None
    peer: str | None = None  # required for meshptp

    def validate(self) -> None:
        if self.mode is not None and self.mode not in MESH_MODES:
            raise ValueError(f"mode must be one of {sorted(MESH_MODES)}")
        if self.mode == "meshptp" and not self.peer:
            raise ValueError("meshptp needs --peer (the other end's MAC address)")
        if self.peer and not all(c in "0123456789abcdefABCDEF:" for c in self.peer):
            raise ValueError("peer must be a MAC address")
        if self.channel is None and self.bandwidth is None and self.mode is None:
            raise ValueError("nothing to change")


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
            warns.append(
                f"RF neighbours not selected will lose their links to the changed nodes: "
                f"{', '.join(sorted(left_out))}")
    return warns


# --------------------------------------------------------------------------- node state

@dataclass
class RadioState:
    radio_index: int          # which radioN carries the mesh
    mode: str
    channel: int | None
    bandwidth: int | None
    channels_by_bw: dict[str, list[int]] = field(default_factory=dict)


def parse_radio_state(raw: str) -> RadioState:
    """Parse the JSON printed by RADIO_STATE_CMD (shape read from aredn.radios)."""
    data = json.loads(raw)
    config = data.get("config") or []
    common = data.get("common") or []
    for i, r in enumerate(config):
        m = (r or {}).get("mode") or {}
        if m.get("mode") in MESH_MODES:
            table: dict[str, list[int]] = {}
            com = common[i] if i < len(common) and isinstance(common[i], dict) else {}
            for bw, chans in (com.get("channels") or {}).items():
                table[str(bw)] = [int(c["number"]) if isinstance(c, dict) else int(c) for c in chans]
            return RadioState(i, m["mode"], m.get("channel"), m.get("bandwidth"), table)
    raise ValueError("no radio is in a mesh mode")


def check_change(state: RadioState, change: RadioChange) -> str | None:
    """Return a reason the change is impossible on this hardware, or None."""
    bw = change.bandwidth if change.bandwidth is not None else state.bandwidth
    ch = change.channel if change.channel is not None else state.channel
    if state.channels_by_bw:
        if str(bw) not in state.channels_by_bw:
            return f"width {bw} MHz not supported (have {', '.join(sorted(state.channels_by_bw, key=int))})"
        if ch not in state.channels_by_bw[str(bw)]:
            return f"channel {ch} not available at {bw} MHz on this radio"
    return None


# --------------------------------------------------------------------------- on-node script

def build_apply_script(radio: int, change: RadioChange, apply_at: int, revert_after: int) -> str:
    sets = []
    if change.mode is not None:
        sets.append(("mode", change.mode))
    if change.channel is not None:
        sets.append(("channel", str(int(change.channel))))
    if change.bandwidth is not None:
        sets.append(("bandwidth", str(int(change.bandwidth))))
    if change.peer:
        sets.append(("peer", change.peer.lower()))
    # Values are validated (ints, whitelisted modes, hex MAC), so plain quoting is safe.
    ucode_sets = " ".join(f'c.setSetting("radio{radio}_{k}", "{v}");' for k, v in sets)
    ucode = ("import * as c from \"aredn.configuration\"; c.prepareChanges(); "
             f"{ucode_sets} c.saveSettings(); c.commitChanges();")
    return f"""#!/bin/sh
# Generated by aredn-fleet. Applies a radio change at a set time and reverts it
# automatically unless the controller confirms within {revert_after}s.
D={STATE_DIR}
mkdir -p $D
rm -f $D/confirmed $D/cancel
cp /etc/config.mesh/setup $D/setup.orig || {{ echo backup-failed > $D/state; exit 1; }}
echo armed > $D/state
now=$(date +%s)
[ {apply_at} -gt "$now" ] && sleep $(({apply_at} - now))
if [ -f $D/cancel ]; then echo cancelled > $D/state; exit 0; fi
ucode -e {shlex.quote(ucode)} > $D/apply.log 2>&1
echo applied > $D/state
[ -f /tmp/reboot-required/reboot ] && echo reboot-required >> $D/apply.log
sleep {revert_after}
if [ -f $D/confirmed ]; then echo confirmed > $D/state; exit 0; fi
cp $D/setup.orig /etc/config.mesh/setup
/usr/local/bin/node-setup > $D/revert.log 2>&1
/usr/local/bin/restart-services >> $D/revert.log 2>&1
echo reverted > $D/state
"""


# --------------------------------------------------------------------------- job

@dataclass
class RadioConfig:
    lead_seconds: float = 90        # time from arming the last node to the switch
    revert_after: int = 600         # node reverts unless confirmed within this
    poll_interval: float = 10
    max_clock_skew: float = 20


@dataclass
class RadioNodeResult:
    node: str
    status: str = "pending"   # would-change, armed, verified, confirmed, failed, unchanged
    detail: str = ""
    before: str = ""


@dataclass
class RadioReport:
    results: dict[str, RadioNodeResult]
    confirmed: bool
    reason: str = ""


def _meshrf(si: dict[str, Any] | None) -> tuple[Any, Any]:
    m = (si or {}).get("meshrf") or {}
    return m.get("channel"), m.get("chanbw")


async def run_radio_change(cluster: list[str], change: RadioChange, connect: Connector,
                           sysinfo: SysinfoFetch, cfg: RadioConfig | None = None,
                           execute: bool = False,
                           sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                           now: Callable[[], float] = time.time,
                           log: Callable[..., None] = lambda *a, **k: None) -> RadioReport:
    """Change one RF cluster together. Dry run (default) does only the read-only preflight."""
    cfg = cfg or RadioConfig()
    change.validate()
    results = {n: RadioNodeResult(n) for n in cluster}
    states: dict[str, RadioState] = {}

    # ---- preflight (read-only)
    for n in cluster:
        r = results[n]
        try:
            s = await connect(n)
            try:
                st = parse_radio_state((await s.run(RADIO_STATE_CMD)).stdout)
                node_time = float((await s.run("date +%s")).stdout.strip() or 0)
            finally:
                await s.close()
        except Exception as exc:  # noqa: BLE001
            r.status, r.detail = "failed", f"preflight: {exc}"
            continue
        r.before = f"radio{st.radio_index} {st.mode} ch {st.channel} @ {st.bandwidth} MHz"
        if abs(node_time - now()) > cfg.max_clock_skew:
            r.status, r.detail = "failed", f"clock off by {node_time - now():+.0f}s; fix NTP first"
            continue
        why = check_change(st, change)
        if why:
            r.status, r.detail = "failed", why
            continue
        states[n] = st
        r.status = "would-change"

    failed = [n for n, r in results.items() if r.status == "failed"]
    if failed:
        return RadioReport(results, False, f"preflight failed on {', '.join(failed)}; nothing changed")
    if not execute:
        return RadioReport(results, False, "dry run")

    # ---- arm every node with the same apply time
    apply_at = int(now() + cfg.lead_seconds)
    log("-", "radio-arm", apply_at=apply_at, change=change.__dict__, nodes=cluster)
    armed: list[str] = []
    for n in cluster:
        script = build_apply_script(states[n].radio_index, change, apply_at, cfg.revert_after)
        try:
            s = await connect(n)
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
                    f.write(script)
                await s.upload(Path(f.name), REMOTE_SCRIPT)
                Path(f.name).unlink(missing_ok=True)
                await s.run(f"nohup sh {REMOTE_SCRIPT} >/dev/null 2>&1 </dev/null &", timeout=20)
            finally:
                await s.close()
            armed.append(n)
            results[n].status = "armed"
        except Exception as exc:  # noqa: BLE001
            results[n].status, results[n].detail = "failed", f"arm: {exc}"
            break
    if len(armed) != len(cluster):
        for n in armed:  # cancel before the switch time; the scripts exit untouched
            try:
                s = await connect(n)
                await s.run(f"touch {STATE_DIR}/cancel")
                await s.close()
                results[n].status, results[n].detail = "unchanged", "cancelled before switch"
            except Exception as exc:  # noqa: BLE001
                results[n].detail = f"CANCEL FAILED, will switch at {apply_at}: {exc}"
        return RadioReport(results, False, "arming failed; switch cancelled")

    # ---- wait for the switch, then verify every node
    await sleep(max(0.0, apply_at - now()) + 5)
    want_ch = change.channel
    want_bw = change.bandwidth
    deadline = apply_at + cfg.revert_after - 60  # leave time to confirm before revert
    pending = set(cluster)
    while pending and now() < deadline:
        for n in sorted(pending):
            ch, bw = _meshrf(await sysinfo(n))
            ok_ch = want_ch is None or ch == want_ch
            ok_bw = want_bw is None or bw == want_bw
            if ch is not None and ok_ch and ok_bw:
                pending.discard(n)
                results[n].status = "verified"
        if pending:
            await sleep(cfg.poll_interval)

    if pending:
        for n in pending:
            results[n].status, results[n].detail = "failed", "not seen on new settings"
        reason = (f"not verified: {', '.join(sorted(pending))}. Confirming nobody; the whole "
                  f"cluster reverts about {cfg.revert_after}s after the switch.")
        log("-", "radio-no-confirm", reason=reason)
        return RadioReport(results, False, reason)

    # ---- confirm everyone
    for n in cluster:
        try:
            s = await connect(n)
            await s.run(f"touch {STATE_DIR}/confirmed")
            await s.close()
            results[n].status = "confirmed"
        except Exception as exc:  # noqa: BLE001
            results[n].status, results[n].detail = "failed", f"confirm: {exc} (this node will revert)"
    ok = all(r.status == "confirmed" for r in results.values())
    log("-", "radio-done", confirmed=ok)
    return RadioReport(results, ok, "" if ok else "some nodes could not be confirmed and will revert")
