import asyncio
import hashlib

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

    def neighbours(self, n):
        return {l.b if l.a == n else l.a for l in self.topo.links if n in (l.a, l.b)}

    def down(self):
        return {n for n, k in self.reboot_polls.items() if k > 0}

    async def sysinfo(self, n):
        if n in self.reboot_polls and self.reboot_polls[n] > 0:
            self.reboot_polls[n] -= 1
            if self.reboot_polls[n] == 0 and n not in self.broken:
                self.version[n] = "4.26.10.0"
            return None
        return {"node": n, "node_details": {"firmware_version": self.version[n], "model": "Rocket M"},
                "link_info": {str(i): {"hostname": f"{x}.local.mesh", "linkType": "RF"}
                              for i, x in enumerate(self.neighbours(n)) if x not in self.down()}}

    async def connect(self, n):
        mesh = self

        class S:
            node = n

            async def run(self, cmd, timeout=60):
                mesh.commands.append((n, cmd))
                if "board_name" in cmd:
                    return CommandResult(0, "ubnt,rocket-m\n", "")
                if "meminfo" in cmd:
                    return CommandResult(0, "MemTotal: 65536 kB\nMemAvailable: 30000 kB\n", "")
                if cmd.startswith("sha256sum"):
                    return CommandResult(0, hashlib.sha256(mesh.uploaded[n]).hexdigest() + "  f\n", "")
                if "aredn_sysupgrade" in cmd:
                    mesh.reboot_polls[n] = 3
                return CommandResult(0, "", "")

            async def upload(self, local, remote):
                mesh.uploaded[n] = local.read_bytes()

            async def close(self):
                pass
        return S()


def release_and_store(tmp_path):
    img = Image("sysupgrade", "aredn-4.26.10.0-ubnt_rocket-m-sysupgrade.bin", SHA, len(PAYLOAD), "u")
    prof = Profile("ubnt_rocket-m", "ath79/generic", ["Ubiquiti Rocket M"], ["ubnt,rocket-m"], [img])
    rel = Release("stable-1", "4.26.10.0", {prof.id: prof})
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
    assert not any("sysupgrade" in c for _, c in mesh.commands)


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
    upgrade_order = [n for n, c in mesh.commands if "aredn_sysupgrade" in c]
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
    assert not any(n == "relay" and "sysupgrade" in c for n, c in mesh.commands)


def test_already_current_is_skipped(tmp_path):
    t = make_topo()
    mesh = FakeMesh(t, version="4.26.10.0")
    rel, store = release_and_store(tmp_path)
    plan = plan_rollout(t, "hub", ["far1"])
    rep = run(run_rollout(plan, rel, store, mesh.connect, mesh.sysinfo, CFG, execute=True, sleep=no_sleep))
    assert rep.results[0].status == "skipped"
