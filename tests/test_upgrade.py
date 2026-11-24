from pathlib import Path
import sys
import pytest
import asyncio
import gzip
import hashlib
import json

from aredn_fleet.firmware import FirmwareStore, Image, Profile, Release
from aredn_fleet.jobs.upgrade import UpgradeConfig, run_rollout
from aredn_fleet.topology import Topology, plan_rollout
from aredn_fleet.transport import CommandResult

PAYLOAD = b"new-firmware"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


class FakeMesh:
    """Nodes that reboot onto the uploaded firmware after a few polls."""

    def __init__(self, topo: Topology, version="4.26.7.0", broken=()):
        self.topo = topo
        self.version = {n: version for n in topo.nodes}
        self.reboot_polls: dict[str, int] = {}
        self.uploaded: dict[str, bytes] = {}
        self.broken = set(broken)  # these come back on the old version
        self.commands: list[tuple[str, str]] = []
        self.reject: set[str] = set()
        self.radios: dict[str, list] = {}        # node -> aredn.radios config (default: 5 GHz mesh)
        self.fail_backup: set[str] = set()
        self.backups: set[str] = set()

    def neighbours(self, n):
        return {l.b if l.a == n else l.a for l in self.topo.links if n in (l.a, l.b)}

    def down(self):
        return {n for n, k in self.reboot_polls.items() if k > 0}

    async def sysinfo(self, n):
        second = getattr(self, "second", {})
        if second.get(n) == "armed":              # older firmware: one more reboot after coming up
            second[n] = "done"
            self.reboot_polls[n] = 2
        if n in self.reboot_polls and self.reboot_polls[n] > 0:
            self.reboot_polls[n] -= 1
            if self.reboot_polls[n] == 0 and n not in self.broken:
                self.version[n] = getattr(self, "next_version", "4.26.10.0")
                if n in getattr(self, "legacy", set()) and n not in second:
                    self.second = {**second, n: "pending"}
            return None
        if second.get(n) == "pending":
            second[n] = "armed"
        return {"node": n, "node_details": {"firmware_version": self.version[n], "model": "Rocket M"},
                "link_info": {str(i): {"hostname": f"{x}.local.mesh", "linkType": "RF"}
                              for i, x in enumerate(self.neighbours(n)) if x not in self.down()}}

    async def connect(self, n):
        mesh = self

        class S:
            node = n

            async def run(self, cmd, timeout=60):
                mesh.commands.append((n, cmd))
                if "configuration.backup' " in cmd:      # STYLE_CMD + ROUTING_CMD probe
                    if n in getattr(mesh, "legacy", set()):
                        return CommandResult(0, "legacy\nkeepfile\nolsrd\n", "")
                    return CommandResult(0, "self-backup\nkeepfile\nbabeld\n", "")
                if cmd.startswith("cat > /tmp/aredn-fleet-legacy-upgrade.sh"):
                    mesh.legacy_script = cmd
                    return CommandResult(0, "", "")
                if "sh /tmp/aredn-fleet-legacy-upgrade.sh" in cmd:
                    mesh.reboot_polls[n] = 3
                    return CommandResult(0, "", "")
                if "arednsysupgrade.conf | while read" in cmd:
                    mesh.backups.add(n)
                    return CommandResult(0, "2048\n", "")
                if "firmwarecheck.sh" in cmd:
                    return CommandResult(0, getattr(mesh, "fwcheck", ""), "")
                if "board_name" in cmd:
                    return CommandResult(0, "ubnt,rocket-m\n", "")
                if "meminfo" in cmd:
                    return CommandResult(0, "MemTotal: 65536 kB\nMemAvailable: 30000 kB\n", "")
                if cmd.startswith("sha256sum"):
                    return CommandResult(0, hashlib.sha256(mesh.uploaded[n]).hexdigest() + "  f\n", "")
                if "validate_firmware_image" in cmd:
                    ok = n not in mesh.reject
                    return CommandResult(0, '{"valid": %s}' % ("true" if ok else "false"), "")
                if "aredn_sysupgrade" in cmd:
                    assert "nohup" not in cmd          # AREDN's BusyBox has no nohup
                    if n not in getattr(mesh, "launch_log", {}):
                        mesh.reboot_polls[n] = 3
                if cmd.startswith("cat /tmp/aredn-fleet-upgrade.log"):
                    return CommandResult(0, getattr(mesh, "launch_log", {}).get(n, ""), "")
                if cmd.startswith("ucode -e 'import * as r"):
                    return CommandResult(0, json.dumps({"config": mesh.radios.get(n, [radio5()])}), "")
                if "radio-and-antenna" in cmd:
                    return CommandResult(0, "single-mesh\n", "")
                if "c.backup" in cmd and n not in mesh.fail_backup:
                    mesh.backups.add(n)
                return CommandResult(0, "", "")

            async def upload(self, local, remote):
                mesh.uploaded[n] = local.read_bytes()

            async def download(self, remote):
                if n not in mesh.backups:
                    raise OSError("no such file")
                return gzip.compress(b"config of " + n.encode())

            async def close(self):
                pass
        return S()


def _launch(c):
    """A command that starts an upgrade (not the read-only probe that greps the script)."""
    return "aredn_sysupgrade" in c and "configuration.backup' " not in c


def radio5(mode="mesh", ch=131, bw=10):
    chans = {str(w): [{"number": c, "label": f"{c} ({f})", "frequency": f}
                      for c, f in [(36, 5180), (48, 5240), (52, 5260), (60, 5300), (131, 5655), (149, 5745)]]
             for w in (10, 20, 40)}
    return {"iface": "wlan0", "mode": {"mode": mode, "channel": ch, "bandwidth": bw, "ssid": "AREDN"},
            "def": {"band": "5GHz"}, "bws": [5, 10, 20, 40, 80], "channels": chans}


def release_and_store(tmp_path, version="4.26.10.0", target="ath79/generic"):
    img = Image("sysupgrade", "aredn-4.26.10.0-ubnt_rocket-m-sysupgrade.bin", SHA, len(PAYLOAD), "u")
    prof = Profile("ubnt_rocket-m", target, ["Ubiquiti Rocket M"], ["ubnt,rocket-m"], [img])
    rel = Release("stable-1", version, {prof.id: prof})
    store = FirmwareStore(tmp_path)
    p = store.path_for(rel, prof, img)
    p.parent.mkdir(parents=True)
    p.write_bytes(PAYLOAD)
    return rel, store


async def no_sleep(_):
    return None


def make_topo():
    t = Topology()
    for a, b in [("hub", "relay"), ("relay", "far1"), ("relay", "far2")]:
        t.add_link(a, b)
    return t


CFG = UpgradeConfig(poll_interval=1, down_timeout=10, up_timeout=20, neighbour_timeout=10, soak_seconds=0)


def run(coro):
    return asyncio.run(coro)


def test_dry_run_changes_nothing(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t)
    rel, store = release_and_store(tmp_path)
    plan = plan_rollout(t, "hub", ["relay", "far1", "far2"], max_parallel=2)
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, CFG, execute=False, sleep=no_sleep))
    assert {r.status for r in rep.results} == {"would-upgrade"}
    assert not mesh.uploaded
    assert not any(_launch(c) for _, c in mesh.commands)


def test_execute_upgrades_edge_first(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t)
    rel, store = release_and_store(tmp_path)
    plan = plan_rollout(t, "hub", ["hub", "relay", "far1", "far2"], max_parallel=2)
    audit = tmp_path / "audit.jsonl"
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, CFG, execute=True,
                          audit=audit, sleep=no_sleep))
    assert not rep.stopped_early, rep.reason
    assert [r.node for r in rep.results][-1] == "hub"
    assert all(v == "4.26.10.0" for v in mesh.version.values())
    upgrade_order = [n for n, c in mesh.commands if _launch(c)]
    assert upgrade_order.index("relay") > max(upgrade_order.index("far1"), upgrade_order.index("far2"))
    assert audit.read_text().count('"event": "ok"') == 4


def test_failure_stops_before_inner_nodes(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t, broken={"far1"})
    rel, store = release_and_store(tmp_path)
    plan = plan_rollout(t, "hub", ["relay", "far1", "far2"], max_parallel=1)
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, CFG, execute=True, sleep=no_sleep))
    assert rep.stopped_early
    assert mesh.version["relay"] == "4.26.7.0"
    assert not any(n == "relay" and _launch(c) for n, c in mesh.commands)


def test_already_current_is_skipped(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t, version="4.26.10.0")
    rel, store = release_and_store(tmp_path)
    plan = plan_rollout(t, "hub", ["far1"])
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, CFG, execute=True, sleep=no_sleep))
    assert rep.results[0].status == "skipped"


def test_node_rejecting_image_never_flashes(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t)
    mesh.reject = {"far1"}
    rel, store = release_and_store(tmp_path)
    plan = plan_rollout(t, "hub", ["far1"])
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, CFG, execute=True, sleep=no_sleep))
    assert rep.results[0].status == "failed" and "rejected image" in rep.results[0].detail
    assert not any(_launch(c) for _, c in mesh.commands)



# --------------------------------------------------------------------------- RC notes

RC = "20261122-5e4131bf"


def rc_mesh(topo, version="4.26.7.0"):
    mesh = FakeMesh(topo, version=version)
    orig = mesh.sysinfo

    async def sysinfo(n):           # nodes come back on the RC build
        si = await orig(n)
        if si and mesh.version[n] == "4.26.10.0":
            mesh.version[n] = RC
            si["node_details"]["firmware_version"] = RC
        return si
    mesh.sysinfo = sysinfo
    return mesh


def test_config_backup_saved_before_flashing(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t)
    rel, store = release_and_store(tmp_path / "fw")
    cfg = UpgradeConfig(**{**CFG.__dict__, "backup_dir": tmp_path / "backups"})
    plan = plan_rollout(t, "hub", ["far1"])
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, cfg, execute=True, sleep=no_sleep))
    r = rep.results[0]
    assert r.status == "ok" and r.backup
    saved = list((tmp_path / "backups" / "far1").glob("*-4.26.7.0.tgz"))
    assert len(saved) == 1 and gzip.decompress(saved[0].read_bytes()) == b"config of far1"
    cmds = [c for n, c in mesh.commands if n == "far1"]
    assert next(i for i, c in enumerate(cmds) if "c.backup" in c) < next(i for i, c in enumerate(cmds) if _launch(c))


def test_no_backup_no_upgrade(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t)
    mesh.fail_backup = {"far1"}
    rel, store = release_and_store(tmp_path / "fw")
    cfg = UpgradeConfig(**{**CFG.__dict__, "backup_dir": tmp_path / "backups"})
    rep = run(run_rollout(plan_rollout(t, "hub", ["far1"]), rel, store, mesh.connect, mesh.sysinfo, cfg,
                          execute=True, sleep=no_sleep))
    assert rep.results[0].status == "failed" and "backup" in rep.results[0].detail
    assert not any(_launch(c) for _, c in mesh.commands)


def test_dry_run_takes_no_backup(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t)
    rel, store = release_and_store(tmp_path / "fw")
    cfg = UpgradeConfig(**{**CFG.__dict__, "backup_dir": tmp_path / "backups"})
    run(run_rollout(plan_rollout(t, "hub", ["far1"]), rel, store, mesh.connect, mesh.sysinfo, cfg, sleep=no_sleep))
    assert not mesh.backups


def test_rc_blocks_removed_ac_channels(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t)
    mesh.radios = {"far1": [radio5(ch=52, bw=10)], "far2": [radio5(ch=60, bw=40)], "relay": [radio5(ch=60, bw=10)]}
    rel, store = release_and_store(tmp_path / "fw", version=RC)
    cfg = UpgradeConfig(**{**CFG.__dict__, "rc_checks": True})
    plan = plan_rollout(t, "hub", ["far1", "far2", "relay"], max_parallel=3)
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, cfg, sleep=no_sleep))
    by = {r.node: r for r in rep.results}
    assert by["far1"].status == "failed" and "channel 52" in by["far1"].detail
    assert by["far2"].status == "failed" and "64" in by["far2"].detail      # 60 @ 40 MHz overlaps 64
    assert by["relay"].status == "would-upgrade"                             # 60 @ 10 MHz is fine


def test_rc_station_needs_its_access_point_on_4267(tmp_path):
    t = Topology()
    t.add_link("hub", "ap")
    t.add_link("ap", "sta")
    rel, store = release_and_store(tmp_path / "fw", version=RC)
    cfg = UpgradeConfig(**{**CFG.__dict__, "rc_checks": True})
    for ap_version, ok in (("4.26.1.0", False), ("4.26.7.0", True)):
        mesh = FakeMesh(t, version="4.26.1.0")
        mesh.version["ap"] = ap_version
        mesh.radios = {"sta": [radio5(mode="meshsta", ch=149, bw=10)], "ap": [radio5(mode="meshap", ch=149, bw=10)]}
        rep = run(run_rollout(plan_rollout(t, "hub", ["sta"]), rel, store, mesh.connect, mesh.sysinfo, cfg,
                              sleep=no_sleep))
        r = rep.results[0]
        assert (r.status == "would-upgrade") == ok, r.detail
        if not ok:
            assert "access point ap runs 4.26.1.0" in r.detail


def test_rc_rollout_upgrades_access_point_then_station(tmp_path):
    t = Topology()
    t.add_link("hub", "ap")
    t.add_link("ap", "sta")
    mesh = rc_mesh(t, version="4.26.1.0")
    mesh.radios = {"sta": [radio5(mode="meshsta", ch=149, bw=10)], "ap": [radio5(mode="meshap", ch=149, bw=10)]}
    rel, store = release_and_store(tmp_path / "fw", version=RC)
    cfg = UpgradeConfig(**{**CFG.__dict__, "rc_checks": True})
    plan = plan_rollout(t, "hub", ["ap", "sta"], must_precede=[("ap", "sta")])
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, cfg, execute=True, sleep=no_sleep))
    assert not rep.stopped_early, rep.reason
    assert [r.node for r in rep.results] == ["ap", "sta"]


def test_ipq40xx_from_4267_gets_a_power_cycle_note(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t, version="4.26.7.0")
    rel, store = release_and_store(tmp_path / "fw", target="ipq40xx/mikrotik")
    rep = run(run_rollout(plan_rollout(t, "hub", ["far1"]), rel, store, mesh.connect, mesh.sysinfo, CFG,
                          sleep=no_sleep))
    assert any("power cycle" in n for n in rep.results[0].notes)
    mesh2 = FakeMesh(t, version="4.26.1.0")
    rep2 = run(run_rollout(plan_rollout(t, "hub", ["far1"]), rel, store, mesh2.connect, mesh2.sysinfo, CFG,
                           sleep=no_sleep))
    assert rep2.results[0].notes == []


def test_radios_reread_after_a_good_upgrade(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t)
    rel, store = release_and_store(tmp_path / "fw")
    seen = {}
    rep = run(run_rollout(plan_rollout(t, "hub", ["far1"]), rel, store, mesh.connect, mesh.sysinfo, CFG,
                          execute=True, sleep=no_sleep, on_inventory=lambda n, inv: seen.setdefault(n, inv)))
    assert rep.results[0].status == "ok" and "far1" in seen


def test_rc_dry_run_accepts_station_when_its_ap_goes_first_in_the_run(tmp_path):
    t = Topology()
    t.add_link("hub", "ap")
    t.add_link("ap", "sta")
    mesh = FakeMesh(t, version="4.26.1.0")
    mesh.radios = {"sta": [radio5(mode="meshsta", ch=149, bw=10)], "ap": [radio5(mode="meshap", ch=149, bw=10)]}
    rel, store = release_and_store(tmp_path / "fw", version=RC)
    cfg = UpgradeConfig(**{**CFG.__dict__, "rc_checks": True})
    ordered = plan_rollout(t, "hub", ["ap", "sta"], must_precede=[("ap", "sta")])
    rep = run(run_rollout(ordered, rel, store, mesh.connect, mesh.sysinfo, cfg, sleep=no_sleep))
    assert {r.node: r.status for r in rep.results} == {"ap": "would-upgrade", "sta": "would-upgrade"}
    # without radio details the planner goes edge-first; the station is blocked with a useful reason
    unordered = plan_rollout(t, "hub", ["ap", "sta"])
    rep = run(run_rollout(unordered, rel, store, mesh.connect, mesh.sysinfo, cfg, sleep=no_sleep))
    sta = next(r for r in rep.results if r.node == "sta")
    assert sta.status == "failed" and "Read radios" in sta.detail


def test_rc_station_check_uses_its_real_access_point(tmp_path):
    # sta is a station of ap on 5 GHz and also meshes with "other" on another radio
    t = Topology()
    t.add_link("hub", "ap")
    t.add_link("ap", "sta")
    t.add_link("sta", "other")
    mesh = FakeMesh(t, version="4.26.1.0")
    mesh.version["ap"] = "4.26.7.0"                    # access point is new enough; "other" is not
    mesh.radios = {"sta": [radio5(mode="meshsta", ch=149, bw=10)]}
    rel, store = release_and_store(tmp_path / "fw", version=RC)
    cfg = UpgradeConfig(**{**CFG.__dict__, "rc_checks": True})
    plan = plan_rollout(t, "hub", ["sta"])
    known = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, cfg, sleep=no_sleep,
                            access_points={"sta": {"ap"}}))
    assert known.results[0].status == "would-upgrade"
    guessed = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, cfg, sleep=no_sleep))
    r = guessed.results[0]
    assert r.status == "failed" and "other runs 4.26.1.0" in r.detail and "read radios" in r.detail


def test_launch_that_never_starts_fails_fast_and_cleans_up(tmp_path):
    t = Topology()
    t.add_link("hub", "a")
    mesh = FakeMesh(t)
    mesh.launch_log = {"a": "ash: nohup: not found\n"}
    rel, store = release_and_store(tmp_path / "fw")
    rep = run(run_rollout(plan_rollout(t, "hub", ["a"]), rel, store, mesh.connect, mesh.sysinfo,
                          CFG, execute=True, sleep=no_sleep))
    r = rep.results[0]
    assert r.status == "failed" and "not found" in r.detail and "unchanged" in r.detail
    assert any(c.startswith("rm -f /tmp/aredn-fleet-upgrade.bin") for _, c in mesh.commands)


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX sh")
def test_detached_runs_under_posix_sh(tmp_path):
    import subprocess
    import time
    from aredn_fleet.transport import detached
    log = tmp_path / "out.log"
    subprocess.run(["sh", "-c", detached("echo started", str(log))], check=True, timeout=10)
    for _ in range(50):
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.05)
    assert log.read_text().strip() == "started"


def test_older_firmware_upgrades_like_its_own_firmware_page(tmp_path):
    t = Topology()
    t.add_link("hub", "ns")
    mesh = FakeMesh(t, version="3.23.8.0")
    mesh.legacy, mesh.next_version = {"ns"}, "3.25.5.1"
    rel, store = release_and_store(tmp_path / "fw", version="3.25.5.1")
    cfg = UpgradeConfig(**{**CFG.__dict__, "backup_dir": tmp_path / "bk", "second_reboot_seconds": 5})
    rep = run(run_rollout(plan_rollout(t, "hub", ["ns"]), rel, store, mesh.connect, mesh.sysinfo, cfg,
                          execute=True, sleep=no_sleep))
    r = rep.results[0]
    assert r.status == "ok", r.detail
    assert r.method == "legacy" and r.routing == "olsr"
    assert any("Firmware page" in n for n in r.notes)
    assert Path(r.backup).exists()                       # same archive, kept on this computer too
    s = mesh.legacy_script
    assert 'set_nvram("nodeupgraded", "1")' in s
    assert "-f /tmp/arednsysupgradebackup.tgz -q /tmp/aredn-fleet-upgrade.bin" in s
    assert '[ "$srv" = dropbear ] && continue' in s
    cmds = [c for _, c in mesh.commands]
    assert next(i for i, c in enumerate(cmds) if "arednsysupgrade.conf | while read" in c) < \
        next(i for i, c in enumerate(cmds) if "sh /tmp/aredn-fleet-legacy-upgrade.sh" in c)
    assert mesh.second["ns"] == "done"                   # waited through the second reboot


def test_olsr_only_node_refused_for_babel_only_target(tmp_path):
    t = Topology()
    t.add_link("hub", "ns")
    mesh = FakeMesh(t, version="3.23.8.0")
    mesh.legacy = {"ns"}
    rel, store = release_and_store(tmp_path / "fw", version="4.26.10.0")
    rep = run(run_rollout(plan_rollout(t, "hub", ["ns"]), rel, store, mesh.connect, mesh.sysinfo, CFG,
                          execute=True, sleep=no_sleep))
    r = rep.results[0]
    assert r.status == "failed" and "only OLSR" in r.detail
    assert not mesh.uploaded


def test_older_firmware_image_check_failure_stops_before_flashing(tmp_path):
    t = Topology()
    t.add_link("hub", "ns")
    mesh = FakeMesh(t, version="3.23.8.0")
    mesh.legacy, mesh.next_version, mesh.fwcheck = {"ns"}, "3.25.5.1", "Unsupported hardware\n"
    rel, store = release_and_store(tmp_path / "fw", version="3.25.5.1")
    rep = run(run_rollout(plan_rollout(t, "hub", ["ns"]), rel, store, mesh.connect, mesh.sysinfo, CFG,
                          execute=True, sleep=no_sleep))
    r = rep.results[0]
    assert r.status == "failed" and "Unsupported hardware" in r.detail
    assert not any("legacy-upgrade.sh" in c for _, c in mesh.commands)


def test_plan_level_block_and_final_release_message(tmp_path):
    t = Topology()
    t.add_link("hub", "ns")
    mesh = FakeMesh(t, version="3.25.5.1")
    rel, store = release_and_store(tmp_path / "fw")
    plan = plan_rollout(t, "hub", ["ns"])
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, CFG, sleep=no_sleep,
                          blocked={"ns": "routing says no"}))
    assert rep.results[0].status == "failed" and rep.results[0].detail == "routing says no"
    rel.profiles = {}            # this release has no image for the hardware
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, CFG, sleep=no_sleep,
                          final_release=lambda board, model: "3.25.5.1"))
    assert "last release is 3.25.5.1" in rep.results[0].detail
