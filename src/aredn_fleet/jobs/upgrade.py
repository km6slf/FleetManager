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
from ..topology import Plan
from ..transport import NodeSession

SysinfoFetch = Callable[[str], Awaitable[dict[str, Any] | None]]
Connector = Callable[[str], Awaitable[NodeSession]]

REMOTE_IMAGE = "/tmp/aredn-fleet-upgrade.bin"


@dataclass
class UpgradeConfig:
    sysupgrade_cmd: str = "/usr/local/bin/aredn_sysupgrade"
    poll_interval: float = 10
    down_timeout: float = 300     # how long to wait for the node to go down
    up_timeout: float = 900       # how long to wait for it to come back
    neighbour_timeout: float = 300
    soak_seconds: float = 180     # TBD: field-tune on the lab mesh
    ram_margin_kb: int = 4096


@dataclass
class NodeResult:
    node: str
    status: str = "pending"   # ok, skipped, would-upgrade, failed
    detail: str = ""
    from_version: str = ""
    to_version: str = ""
    profile: str = ""


class AuditLog:
    def __init__(self, path: Path | None):
        self.path = path

    def __call__(self, node: str, event: str, **data: Any) -> None:
        if not self.path:
            return
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "node": node, "event": event, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")


def _neighbours(sysinfo: dict[str, Any] | None) -> set[str]:
    if not sysinfo:
        return set()
    out = set()
    for e in (sysinfo.get("link_info") or {}).values():
        if e.get("hostname") and str(e.get("linkType", "")).upper() in ("RF", "DTD", ""):
            out.add(e["hostname"].lower().removesuffix(".local.mesh"))
    return out


def _fw(sysinfo: dict[str, Any] | None) -> str:
    return str(((sysinfo or {}).get("node_details") or {}).get("firmware_version", ""))


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

        matches = match_profile(self.release, board_name=board, model=model)
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

        meminfo = (await s.run("cat /proc/meminfo")).stdout
        avail = _kb(meminfo, "MemAvailable:") or _kb(meminfo, "MemFree:")
        need = image.size // 1024 + self.cfg.ram_margin_kb
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
        self.log(self.node, "upload", image=image.name)
        await s.upload(local, REMOTE_IMAGE)
        sha = (await s.run(f"sha256sum {REMOTE_IMAGE}")).stdout.split()[:1]
        if sha != [image.sha256]:
            await s.run(f"rm -f {REMOTE_IMAGE}")
            return self._fail(res, f"sha256 mismatch on node ({sha})")

        neighbours = _neighbours(before)
        self.log(self.node, "sysupgrade", neighbours=sorted(neighbours))
        await s.run(f"nohup {self.cfg.sysupgrade_cmd} {REMOTE_IMAGE} "
                    f">/tmp/aredn-fleet-upgrade.log 2>&1 </dev/null &", timeout=30)

        if not await self._wait(lambda si: si is None, self.cfg.down_timeout):
            return self._fail(res, "node never went down; check /tmp/aredn-fleet-upgrade.log on the node")
        self.log(self.node, "down")

        if not await self._wait(lambda si: si is not None, self.cfg.up_timeout):
            return self._fail(res, "node did not come back within up_timeout")
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
        return res

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
                      on_result: Callable[[NodeResult], None] | None = None) -> RolloutReport:
    cfg = cfg or UpgradeConfig()
    log = AuditLog(audit)
    log("-", "rollout-start", execute=execute, version=release.version, waves=plan.waves)
    results: list[NodeResult] = []
    finished: list[str] = []

    for i, wave in enumerate(plan.waves):
        jobs = [NodeUpgrade(n, release, store, connect, sysinfo, cfg, log,
                            plan.dependents.get(n, set()), sleep, clock) for n in wave]
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
