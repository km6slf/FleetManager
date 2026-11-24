"""Firmware upgrade job: per-node steps and the wave-by-wave rollout runner.

Per node:
  1. connect (credential probing), read board name, current firmware, free RAM and /tmp
  2. resolve exactly one firmware profile for the board, otherwise refuse
  3. skip if already on the target version
  4. upload to /tmp, verify sha256 on the node
  5. record RF/DTD neighbours, start ``aredn_sysupgrade`` detached
  6. wait for the node to drop, then return
  7. verify firmware version, neighbours, and every dependent is reachable again

Rollout:
  * waves come from ``topology.plan_rollout`` (edge inward, root last)
  * nodes within a wave run concurrently
  * the first failure stops the rollout; nothing nearer the root is touched
  * soak between waves, then re-check everything already finished
  * dry run (the default) does only the read-only steps 1 to 3
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..firmware import FirmwareStore, Release, match_profile
from ..rc import at_least_4267, ipq40xx_freeze_note, removed_channel_conflict, station_radios
from .radio import read_node_radios
from ..topology import Plan
from ..discovery import link_entries
from ..routing import BABEL, OLSR, ROUTING_CMD, routing_from_daemons, routing_of
from ..transport import NodeSession, detached

SysinfoFetch = Callable[[str], Awaitable[dict[str, Any] | None]]
Connector = Callable[[str], Awaitable[NodeSession]]

REMOTE_IMAGE = "/tmp/aredn-fleet-upgrade.bin"
UPGRADE_LOG = "/tmp/aredn-fleet-upgrade.log"


@dataclass
class UpgradeConfig:
    sysupgrade_cmd: str = "/usr/local/bin/aredn_sysupgrade"
    poll_interval: float = 10
    down_timeout: float = 300     # how long to wait for the node to go down
    up_timeout: float = 900       # how long to wait for it to come back
    neighbour_timeout: float = 300
    soak_seconds: float = 180     # TBD: field-tune on the lab mesh
    ram_margin_kb: int = 4096
    launch_check_seconds: float = 8
    backup_dir: Path | None = None   # save each node's config here before flashing
    rc_checks: bool = False          # target is the release candidate (or later)
    # Older firmware applies its settings after the upgrade and reboots a second time.
    second_reboot_seconds: float = 240


@dataclass
class NodeResult:
    node: str
    status: str = "pending"   # ok, skipped, would-upgrade, failed
    detail: str = ""
    from_version: str = ""
    to_version: str = ""
    profile: str = ""
    notes: list[str] = field(default_factory=list)   # non-blocking warnings for the review
    backup: str = ""
    routing: str = ""        # olsr / dual / babel as seen on the node (running daemons)
    method: str = ""         # "current" or "legacy" upgrade procedure


class AuditLog:
    def __init__(self, path: Path | None):
        self.path = path

    def __call__(self, node: str, event: str, **data: Any) -> None:
        if not self.path:
            return
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "node": node, "event": event, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")


def _rf_neighbours(sysinfo: dict[str, Any] | None) -> set[str]:
    out = set()
    for e in link_entries(sysinfo or {}):
        if e.get("hostname") and str(e.get("linkType", "")).upper() == "RF":
            out.add(e["hostname"].lower().removesuffix(".local.mesh"))
    return out


def _neighbours(sysinfo: dict[str, Any] | None) -> set[str]:
    if not sysinfo:
        return set()
    out = set()
    for e in link_entries(sysinfo or {}):
        if e.get("hostname") and str(e.get("linkType", "")).upper() in ("RF", "DTD", ""):
            out.add(e["hostname"].lower().removesuffix(".local.mesh"))
    return out


def _fw(sysinfo: dict[str, Any] | None) -> str:
    return str(((sysinfo or {}).get("node_details") or {}).get("firmware_version", ""))


def _validation_ok(output: str) -> bool | None:
    """Parse ``validate_firmware_image`` JSON; None if it can't be parsed."""
    try:
        return bool(json.loads(output).get("valid"))
    except (ValueError, AttributeError):
        return None


def _kb(output: str, key: str) -> int:
    for line in output.splitlines():
        if line.startswith(key):
            return int(line.split()[1])
    return 0


@dataclass
class NodeUpgrade:
    node: str
    release: Release
    store: FirmwareStore
    connect: Connector
    sysinfo: SysinfoFetch
    cfg: UpgradeConfig
    log: AuditLog
    dependents: set[str] = field(default_factory=set)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    on_inventory: Callable[[str, Any], None] | None = None
    earlier: set[str] = field(default_factory=set)    # nodes this run upgrades before this one
    in_run: set[str] = field(default_factory=set)     # every node in this run
    access_points: set[str] | None = None             # this station's PtMP AP(s); None = unknown
    blocked: str | None = None        # refused before logging in (e.g. routing migration order)
    final_release: Callable[[str, str], str | None] | None = None   # (board, model) -> last release

    async def run(self, execute: bool) -> NodeResult:
        res = NodeResult(self.node, to_version=self.release.version)
        try:
            session = await self.connect(self.node)
        except Exception as exc:  # noqa: BLE001
            return self._fail(res, f"connect: {exc}")
        try:
            return await self._run(session, res, execute)
        except Exception as exc:  # noqa: BLE001
            return self._fail(res, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - node may already be rebooting
                pass

    def _fail(self, res: NodeResult, why: str) -> NodeResult:
        res.status, res.detail = "failed", why
        self.log(self.node, "failed", detail=why)
        return res

    async def _run(self, s: NodeSession, res: NodeResult, execute: bool) -> NodeResult:
        board = (await s.run("cat /tmp/sysinfo/board_name")).stdout.strip()
        before = await self.sysinfo(self.node)
        res.from_version = _fw(before)
        model = str(((before or {}).get("node_details") or {}).get("model", ""))
        if self.blocked:
            return self._fail(res, self.blocked)

        # How this node's firmware upgrades, and which routing daemons it runs, from the node.
        probe = (await s.run(STYLE_CMD + "; " + ROUTING_CMD)).stdout
        legacy = "legacy" in probe
        res.method = "legacy" if legacy else "current"
        res.routing = routing_from_daemons(probe) or ""
        if legacy:
            if "keepfile" not in probe:
                return self._fail(res, "older firmware without /etc/arednsysupgrade.conf: can't tell "
                                       "which settings to keep, so not upgrading it")
            res.notes.append("Older firmware: upgraded the way its own Firmware page does it "
                             "(settings from /etc/arednsysupgrade.conf, then a second reboot to "
                             "apply them).")
        if routing_of(self.release.version) == BABEL and res.routing == OLSR:
            return self._fail(res, "the node runs only OLSR: going straight to Babel-only firmware "
                                   "would cut it off. Upgrade it to an OLSR + Babel release "
                                   "(3.25.5.0 or later 3.25.x) first.")

        matches = match_profile(self.release, board_name=board, model=model)
        if not matches and self.final_release:
            try:
                last = await asyncio.to_thread(self.final_release, board, model)
            except Exception:  # noqa: BLE001 - only improves the message
                last = None
            if last:
                return self._fail(res, f"{self.release.version} has no image for this hardware "
                                       f"({board or model}). Its last release is {last}: upgrade it "
                                       f"to {last} in a separate run.")
        if len(matches) != 1:
            names = ", ".join(p.id for p in matches) or "none"
            return self._fail(res, f"board {board!r} / model {model!r} matched {names}; need exactly one")
        profile = matches[0]
        res.profile = profile.id
        image = profile.image("sysupgrade")
        if image is None:
            return self._fail(res, f"no sysupgrade image for {profile.id} in {self.release.version}")

        if res.from_version == self.release.version:
            res.status, res.detail = "skipped", "already on target version"
            return res

        if note := ipq40xx_freeze_note(profile.target, res.from_version):
            res.notes.append(note)
        if self.cfg.rc_checks:
            why = await self._rc_preflight(s, before)
            if why:
                return self._fail(res, why)

        meminfo = (await s.run("cat /proc/meminfo")).stdout
        avail = _kb(meminfo, "MemAvailable:") or _kb(meminfo, "MemFree:")
        # A copy left in /tmp by an earlier failed run is overwritten, so its RAM counts as free.
        try:
            leftover = int((await s.run(f"wc -c < {REMOTE_IMAGE} 2>/dev/null")).stdout.strip() or 0)
        except (ValueError, AttributeError):
            leftover = 0
        if avail:
            avail += leftover // 1024
        need = image.size // 1024 + self.cfg.ram_margin_kb + (256 if legacy else 0)
        if avail and avail < need:
            return self._fail(res, f"not enough RAM for image in /tmp: {avail} kB free, need {need} kB")

        local = self.store.path_for(self.release, profile, image)
        if not local.exists():
            return self._fail(res, f"image not downloaded: {local} (run `aredn-fleet firmware fetch`)")

        if not execute:
            res.status = "would-upgrade"
            res.detail = f"{res.from_version or '?'} -> {self.release.version} via {image.name}"
            return res

        # ---- point of no return starts below; log every step
        if legacy:
            # The settings archive the old Firmware page builds; sysupgrade restores from it.
            try:
                await self._legacy_archive(s)
            except Exception as exc:  # noqa: BLE001
                return self._fail(res, f"couldn't save the node's settings ({exc}); not upgrading")
        if self.cfg.backup_dir:
            try:
                res.backup = await (self._legacy_backup(s, res.from_version) if legacy
                                    else self._backup(s, res.from_version))
            except Exception as exc:  # noqa: BLE001
                return self._fail(res, f"config backup failed ({exc}); not upgrading without one")
            self.log(self.node, "backup", path=res.backup)
        self.log(self.node, "upload", image=image.name)
        await s.upload(local, REMOTE_IMAGE)
        sha = (await s.run(f"sha256sum {REMOTE_IMAGE}")).stdout.split()[:1]
        if sha != [image.sha256]:
            await s.run(f"rm -f {REMOTE_IMAGE}")
            return self._fail(res, f"sha256 mismatch on node ({sha})")

        # aredn_sysupgrade checks the image itself, but we launch it detached
        # and would lose that verdict, so run the same validator first.
        # Without --force it refuses a device mismatch; we never pass --force.
        check = await s.run(f"/usr/libexec/validate_firmware_image {REMOTE_IMAGE}")
        valid = _validation_ok(check.stdout)
        if valid is False:
            await s.run(f"rm -f {REMOTE_IMAGE}")
            return self._fail(res, f"node rejected image: {check.stdout.strip()[:300]}")

        if legacy:
            # The old Firmware page also runs its own image check after dropping caches.
            chk = (await s.run("echo 3 > /proc/sys/vm/drop_caches; "
                               "[ -x /usr/local/bin/firmwarecheck.sh ] && "
                               f"/usr/local/bin/firmwarecheck.sh {REMOTE_IMAGE}")).stdout.strip()
            if chk:
                await s.run(f"rm -f {REMOTE_IMAGE} {LEGACY_ARCHIVE}")
                return self._fail(res, f"node rejected image: {chk[:300]}")

        neighbours = _neighbours(before)
        self.log(self.node, "sysupgrade", neighbours=sorted(neighbours), validated=valid,
                 method=res.method)
        if legacy:
            await s.run(f"cat > {LEGACY_SCRIPT} <<'FLEETEOF'\n"
                        f"{legacy_upgrade_script(self.cfg.sysupgrade_cmd)}\nFLEETEOF", timeout=20)
            await s.run(detached(f"sh {LEGACY_SCRIPT}", UPGRADE_LOG), timeout=30)
        else:
            await s.run(detached(f"{self.cfg.sysupgrade_cmd} {REMOTE_IMAGE}", UPGRADE_LOG), timeout=30)

        # aredn_sysupgrade backs up the config (it stops babel first) before
        # flashing. A failure there prints "ERROR:" and exits; catch it, or a
        # launch that never started, while the SSH session still works.
        await self.sleep(self.cfg.launch_check_seconds)
        try:
            log = (await s.run(f"cat {UPGRADE_LOG}", timeout=10)).stdout
        except Exception:  # noqa: BLE001 - node already going down is the normal case
            log = ""
        if _launch_failed(log):
            stopped = "ERROR" in log
            try:
                await s.run(f"rm -f {REMOTE_IMAGE}", timeout=10)
            except Exception:  # noqa: BLE001
                pass
            if legacy and stopped:
                why = (" (nothing was flashed; the node's services and settings were put back, "
                       "and its firmware is unchanged)")
            elif stopped:
                why = " (babel may be stopped; node may need a reboot)"
            else:
                why = " (nothing was flashed; the node is unchanged)"
            return self._fail(res, f"aredn_sysupgrade failed: {log.strip()[:300]}{why}")

        self.log(self.node, "launched", log=log.strip()[-500:])
        if not await self._wait(lambda si: si is None, self.cfg.down_timeout):
            return self._fail(res, "node never went down, so the upgrade didn't run to the flash. "
                                   + await self._node_upgrade_log())
        self.log(self.node, "down")

        if not await self._wait(lambda si: si is not None, self.cfg.up_timeout):
            hint = (" This is probably the IPQ40xx reboot freeze: power-cycle the node."
                    if any("IPQ40xx" in n for n in res.notes) else "")
            return self._fail(res, "node did not come back within up_timeout." + hint)
        if legacy:
            # Old firmware reboots once more, soon after coming up, while it applies the kept
            # settings. Let that happen before judging the result.
            self.log(self.node, "up-first", firmware=_fw(await self.sysinfo(self.node)))
            if await self._wait(lambda si: si is None, self.cfg.second_reboot_seconds):
                if not await self._wait(lambda si: si is not None, self.cfg.up_timeout):
                    return self._fail(res, "node didn't come back after its second reboot "
                                           "(the one that applies the kept settings)")
                self.log(self.node, "second-reboot-done")
        after = await self.sysinfo(self.node)
        if _fw(after) != self.release.version:
            return self._fail(res, f"came back on {_fw(after)!r}, expected {self.release.version}")
        self.log(self.node, "up", firmware=_fw(after))

        missing: set[str] = set()

        async def neighbours_back(si):
            nonlocal missing
            missing = neighbours - _neighbours(si)
            return not missing

        if not await self._wait_async(neighbours_back, self.cfg.neighbour_timeout):
            return self._fail(res, f"neighbours not back: {', '.join(sorted(missing))}")

        unreachable = [d for d in sorted(self.dependents) if await self.sysinfo(d) is None]
        if unreachable:
            return self._fail(res, f"dependents unreachable: {', '.join(unreachable)}")

        res.status, res.detail = "ok", f"{res.from_version} -> {self.release.version}"
        self.log(self.node, "ok")
        await self._reread_radios()
        return res

    async def _rc_preflight(self, s: NodeSession, before: dict[str, Any] | None) -> str | None:
        """Checks from AREDN's RC notes, on live data from the node."""
        inv = await read_node_radios(s)
        for r in inv.radios:
            if why := removed_channel_conflict(r):
                return why
        if station_radios(inv):
            # Its PtMP access point(s), from the neighbours' radio details. If those aren't
            # known, be cautious and treat every RF neighbour as a possible access point.
            guessed = self.access_points is None
            aps = (_rf_neighbours(before) if guessed else self.access_points)
            hint = (" (couldn't tell which RF neighbour is its access point; read radios for its "
                    "neighbours to narrow this down)") if guessed else ""
            for ap in sorted(aps):
                if ap in self.earlier:
                    continue        # upgraded earlier in this run (a failure there stops the run)
                fw = _fw(await self.sysinfo(ap))
                if fw and at_least_4267(fw):
                    continue
                if ap in self.in_run:
                    return (f"Mesh Station: its PtMP access point {ap} is in this run but scheduled "
                            f"after it, because the radio details of {ap} or this node haven't been "
                            f"read. Read radios for both and review again, so {ap} goes first.{hint}")
                if not fw:
                    return (f"Mesh Station: can't check its PtMP access point {ap}; it must run "
                            f"4.26.7.0 or the RC before this node is upgraded.{hint}")
                return (f"Mesh Station: its PtMP access point {ap} runs {fw}. Upgrade {ap} to "
                        f"4.26.7.0 or the RC first (AREDN RC notes).{hint}")
        return None

    async def _backup(self, s: NodeSession, from_version: str) -> str:
        remote = "/tmp/aredn-fleet-backup.tgz"
        await s.run(f"ucode -e 'import * as c from \"aredn.configuration\"; c.backup(\"{remote}\");'",
                    timeout=60)
        data = await s.download(remote)
        await s.run(f"rm -f {remote}")
        if len(data) < 20 or data[:2] != b"\x1f\x8b":
            raise ValueError("the node didn't produce a valid backup file")
        d = Path(self.cfg.backup_dir) / self.node
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"{self.node}-{time.strftime('%Y%m%d-%H%M%S')}-{from_version or 'unknown'}.tgz"
        out.write_bytes(data)
        return str(out)

    async def _legacy_archive(self, s: NodeSession) -> None:
        """Build /tmp/arednsysupgradebackup.tgz exactly as the old Firmware page does: every
        existing, non-comment path in /etc/arednsysupgrade.conf, archived with tar."""
        out = (await s.run(
            "grep -v '^#' /etc/arednsysupgrade.conf | while read f; do "
            "[ -n \"$f\" ] && [ -e \"$f\" ] && echo \"$f\"; done > /tmp/sysupgradefilelist && "
            f"tar -czf {LEGACY_ARCHIVE} -T /tmp/sysupgradefilelist >/dev/null 2>&1 && "
            f"rm -f /tmp/sysupgradefilelist && wc -c < {LEGACY_ARCHIVE}", timeout=60)).stdout.strip()
        if not out.isdigit() or int(out) < 20:
            raise ValueError("tar didn't produce the settings archive")

    async def _legacy_backup(self, s: NodeSession, from_version: str) -> str:
        """Keep a copy of that same archive on this computer (it stays on the node for sysupgrade)."""
        data = await s.download(LEGACY_ARCHIVE)
        if len(data) < 20 or data[:2] != b"\x1f\x8b":
            raise ValueError("the node's settings archive isn't a valid backup file")
        d = Path(self.cfg.backup_dir) / self.node
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"{self.node}-{time.strftime('%Y%m%d-%H%M%S')}-{from_version or 'unknown'}.tgz"
        out.write_bytes(data)
        return str(out)

    async def _reread_radios(self) -> None:
        """Channel lists can change with firmware (the RC drops some); refresh them."""
        if not self.on_inventory:
            return
        try:
            s = await self.connect(self.node)
            try:
                self.on_inventory(self.node, await read_node_radios(s))
            finally:
                await s.close()
        except Exception:  # noqa: BLE001 - not worth failing a good upgrade over
            self.log(self.node, "radio-reread-failed")

    async def _node_upgrade_log(self) -> str:
        """What aredn_sysupgrade said, for a failure message (best effort, read-only)."""
        try:
            s = await self.connect(self.node)
            try:
                out = (await s.run(f"cat {UPGRADE_LOG} 2>&1; echo; "
                                   f"ls -l {REMOTE_IMAGE} 2>&1; "
                                   "ps w | grep -E 'sysupgrade' | grep -v grep", timeout=15)).stdout
            finally:
                await s.close()
        except Exception as exc:  # noqa: BLE001
            return f"(couldn't read the node's upgrade log: {exc})"
        out = " | ".join(x.strip() for x in out.splitlines() if x.strip())
        self.log(self.node, "upgrade-log", text=out[-2000:])
        return f"Node's upgrade log: {out[-600:] or '(empty)'}"

    async def _wait(self, pred: Callable[[Any], bool], timeout: float) -> bool:
        async def apred(si):
            return pred(si)
        return await self._wait_async(apred, timeout)

    async def _wait_async(self, pred, timeout: float) -> bool:
        deadline = self.clock() + timeout
        while True:
            if await pred(await self.sysinfo(self.node)):
                return True
            if self.clock() >= deadline:
                return False
            await self.sleep(self.cfg.poll_interval)


# How the node's own aredn_sysupgrade behaves: newer ones archive the settings themselves.
STYLE_CMD = ("grep -qE 'arednsysupgradebackup|configuration.backup' "
             "/usr/local/bin/aredn_sysupgrade 2>/dev/null && echo self-backup || echo legacy; "
             "[ -f /etc/arednsysupgrade.conf ] && echo keepfile; true")
LEGACY_ARCHIVE = "/tmp/arednsysupgradebackup.tgz"
LEGACY_SCRIPT = "/tmp/aredn-fleet-legacy-upgrade.sh"


def legacy_upgrade_script(sysupgrade_cmd: str) -> str:
    """Run detached on older firmware, mirroring its Firmware page ("Keep settings"):

    1. set the nodeupgraded flag with the node's own Lua helper (it re-applies settings at boot);
    2. stop the services the node's upgrade_prepare.sh lists, to free RAM, except dropbear so
       this tool can still read the log if something fails before the flash;
    3. aredn_sysupgrade -f <settings archive> -q <image>.
    sysupgrade "returns" even when it worked (PID 1 execs the stage-2 upgrader and the ubus
    call is cut off), so the script only undoes 1 and 2 if PID 1 is still procd afterwards.
    """
    return f"""#!/bin/sh
lua -e 'require("aredn.info").set_nvram("nodeupgraded", "1")' || {{ echo "ERROR: couldn't set nodeupgraded"; exit 1; }}
for S in $(sed -n 's/^SERVICES="\(.*\)"$/\1/p' /usr/local/bin/upgrade_prepare.sh 2>/dev/null); do
  srv=${{S%%:*}}; d=$(echo "$S" | cut -d: -f2 -s)
  [ "$srv" = dropbear ] && continue
  [ -n "$d" ] && killall -KILL "$d" 2>/dev/null
  [ -x "/etc/init.d/$srv" ] && "/etc/init.d/$srv" stop >/dev/null 2>&1
done
echo 3 > /proc/sys/vm/drop_caches
{sysupgrade_cmd} -f {LEGACY_ARCHIVE} -q {REMOTE_IMAGE}
rc=$?
# On this firmware a good hand-off *looks* like a failure: procd (PID 1) execs the stage-2
# upgrader, which drops sysupgrade's ubus call ("Connection failed", 246). Undoing anything
# now would delete the image stage 2 is about to flash. Only undo if PID 1 is still procd.
sleep 3
if [ "$(cat ${{FLEET_PID1_COMM:-/proc/1/comm}} 2>/dev/null)" != procd ]; then
  echo "handed to stage 2 (sysupgrade returned $rc); flashing"
  exit 0
fi
echo "ERROR: sysupgrade returned $rc without flashing"
lua -e 'require("aredn.info").set_nvram("nodeupgraded", "0")'
/usr/local/bin/upgrade_prepare.sh restore >/dev/null 2>&1
rm -f {REMOTE_IMAGE} {LEGACY_ARCHIVE}"""


def _launch_failed(log: str) -> bool:
    """The upgrade log shows it never got going (missing command, script error)."""
    return any(s in log for s in ("ERROR", "not found", "No such file", "Permission denied",
                                  "can't execute"))


@dataclass
class RolloutReport:
    results: list[NodeResult]
    stopped_early: bool
    reason: str = ""


async def run_rollout(plan: Plan, release: Release, store: FirmwareStore, connect: Connector,
                      sysinfo: SysinfoFetch, cfg: UpgradeConfig | None = None,
                      execute: bool = False, audit: Path | None = None,
                      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                      clock: Callable[[], float] = time.monotonic,
                      on_result: Callable[[NodeResult], None] | None = None,
                      should_stop: Callable[[], bool] | None = None,
                      on_inventory: Callable[[str, Any], None] | None = None,
                      access_points: dict[str, set[str]] | None = None,
                      blocked: dict[str, str] | None = None,
                      final_release: Callable[[str, str], str | None] | None = None) -> RolloutReport:
    """``access_points`` maps a Mesh Station to its PtMP access point(s), where known.
    ``blocked`` refuses nodes for plan-level reasons (e.g. the OLSR -> Babel migration order).
    ``final_release`` looks up the last release for a node's hardware, for a clearer message."""
    cfg = cfg or UpgradeConfig()
    log = AuditLog(audit)
    log("-", "rollout-start", execute=execute, version=release.version, waves=plan.waves)
    results: list[NodeResult] = []
    finished: list[str] = []

    in_run = {n for w in plan.waves for n in w}
    for i, wave in enumerate(plan.waves):
        earlier = {n for w in plan.waves[:i] for n in w}
        if should_stop and should_stop():
            reason = f"stopped by operator before wave {i + 1}"
            log("-", "rollout-stopped", reason=reason)
            return RolloutReport(results, True, reason)
        jobs = [NodeUpgrade(n, release, store, connect, sysinfo, cfg, log,
                            plan.dependents.get(n, set()), sleep, clock, on_inventory, earlier, in_run,
                            (access_points or {}).get(n), (blocked or {}).get(n), final_release)
                for n in wave]
        wave_results = await asyncio.gather(*(j.run(execute) for j in jobs))
        for r in wave_results:
            results.append(r)
            if on_result:
                on_result(r)
        failed = [r for r in wave_results if r.status == "failed"]
        if failed and execute:
            reason = f"wave {i + 1} failed on {', '.join(r.node for r in failed)}; stopped"
            log("-", "rollout-stopped", reason=reason)
            return RolloutReport(results, True, reason)
        finished += [r.node for r in wave_results if r.status == "ok"]

        if execute and i < len(plan.waves) - 1 and any(r.status == "ok" for r in wave_results):
            await sleep(cfg.soak_seconds)
            lost = [n for n in finished if await sysinfo(n) is None]
            if lost:
                reason = f"after soak, previously upgraded node(s) unreachable: {', '.join(lost)}"
                log("-", "rollout-stopped", reason=reason)
                return RolloutReport(results, True, reason)

    log("-", "rollout-done")
    return RolloutReport(results, False)
