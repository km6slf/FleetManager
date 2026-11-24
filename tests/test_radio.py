import asyncio
import json
import re
import subprocess

import pytest

from aredn_fleet.jobs.radio import (RadioChange, RadioConfig, build_apply_script, check_change,
                                    cluster_warnings, parse_radio_state, rf_clusters,
                                    run_radio_change)
from aredn_fleet.topology import Topology
from aredn_fleet.transport import CommandResult

STATE = {
    "config": [
        {"mode": {"mode": "mesh", "channel": 131, "bandwidth": 20}},
        {"mode": {"mode": "wan"}},
    ],
    "common": [
        {"channels": {"5": [{"number": 131}, {"number": 132}],
                      "10": [{"number": 131}, {"number": 132}, {"number": 133}],
                      "20": [{"number": 131}, {"number": 133}]}},
        {"channels": {}},
    ],
}


def test_parse_and_check():
    st = parse_radio_state(json.dumps(STATE))
    assert (st.radio_index, st.mode, st.channel, st.bandwidth) == (0, "mesh", 131, 20)
    assert check_change(st, RadioChange(channel=133, bandwidth=10)) is None
    assert "not available" in check_change(st, RadioChange(channel=132))
    assert "not supported" in check_change(st, RadioChange(bandwidth=40))


def test_change_validation():
    with pytest.raises(ValueError):
        RadioChange(mode="lan").validate()
    with pytest.raises(ValueError):
        RadioChange(mode="meshptp").validate()
    with pytest.raises(ValueError):
        RadioChange().validate()
    RadioChange(mode="meshptp", peer="08:55:31:0c:73:56").validate()


def test_script_is_valid_shell():
    s = build_apply_script(0, RadioChange(channel=133, bandwidth=10), 2_000_000_000, 600)
    subprocess.run(["sh", "-n"], input=s, text=True, check=True)
    assert 'c.setSetting("radio0_channel", "133")' in s
    assert 'c.setSetting("radio0_bandwidth", "10")' in s
    assert "setup.orig /etc/config.mesh/setup" in s
    assert "/etc/aredn-fleet-radio" in s and "rc.local" in s


def test_rf_clusters_split_on_dtd():
    t = Topology()
    t.add_link("a", "b", "RF")
    t.add_link("b", "c", "DTD")
    t.add_link("c", "d", "RF")
    assert sorted(map(sorted, rf_clusters(t, {"a", "b", "c", "d"}))) == [["a", "b"], ["c", "d"]]
    assert cluster_warnings(t, {"a"}) and "b" in cluster_warnings(t, {"a"})[0]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += max(s, 1)


class FakeCluster:
    def __init__(self, nodes, clock, broken=(), skew=0):
        self.clock = clock
        self.ch = {n: (131, 20) for n in nodes}
        self.apply_at = {}
        self.pending = {}
        self.confirmed = set()
        self.cancelled = set()
        self.broken = set(broken)
        self.skew = skew
        self._script = {}
        self.no_rclocal = set()

    async def sysinfo(self, n):
        if n in self.apply_at and self.clock.now() >= self.apply_at[n] and n not in self.cancelled \
                and n not in self.broken:
            self.ch[n] = self.pending[n]
        ch, bw = self.ch[n]
        return {"meshrf": {"channel": ch, "chanbw": bw}}

    async def connect(self, n):
        fc = self

        class S:
            async def run(self, cmd, timeout=60):
                if cmd.startswith("ucode"):
                    return CommandResult(0, json.dumps(STATE), "")
                if "rc.local" in cmd and cmd.startswith("[ -f"):
                    return CommandResult(0, "" if n in fc.no_rclocal else "ok\n", "")
                if cmd.startswith("date"):
                    return CommandResult(0, str(int(fc.clock.now() + fc.skew)), "")
                if cmd.startswith("nohup"):
                    script = fc._script[n]
                    at = int(re.search(r"\[ (\d+) -gt", script).group(1))
                    ch = int(script.split('radio0_channel", "')[1].split('"')[0])
                    bw = int(script.split('radio0_bandwidth", "')[1].split('"')[0])
                    fc.apply_at[n], fc.pending[n] = at, (ch, bw)
                if "/confirmed" in cmd:
                    fc.confirmed.add(n)
                if "/cancel" in cmd:
                    fc.cancelled.add(n)
                return CommandResult(0, "", "")

            async def upload(self, local, remote):
                fc._script[n] = local.read_text()

            async def close(self):
                pass
        return S()



CFG = RadioConfig(lead_seconds=30, revert_after=600, poll_interval=10)


def go(fc, clock, execute=True, change=RadioChange(channel=133, bandwidth=10)):
    return asyncio.run(run_radio_change(sorted(fc.ch), change, fc.connect, fc.sysinfo, CFG,
                                        execute=execute, sleep=clock.sleep, now=clock.now))


def test_dry_run_reads_only():
    clock = Clock()
    fc = FakeCluster(["a", "b"], clock)
    rep = go(fc, clock, execute=False)
    assert {r.status for r in rep.results.values()} == {"would-change"}
    assert not fc.apply_at


def test_whole_cluster_switches_and_is_confirmed():
    clock = Clock()
    fc = FakeCluster(["a", "b", "c"], clock)
    rep = go(fc, clock)
    assert rep.confirmed, rep.reason
    assert len(set(fc.apply_at.values())) == 1          # one shared switch time
    assert fc.confirmed == {"a", "b", "c"}


def test_one_node_failing_means_nobody_confirms():
    clock = Clock()
    fc = FakeCluster(["a", "b", "c"], clock, broken={"b"})
    rep = go(fc, clock)
    assert not rep.confirmed and "b" in rep.reason
    assert fc.confirmed == set()                         # everyone reverts together


def test_clock_skew_blocks_before_anything_changes():
    clock = Clock()
    fc = FakeCluster(["a"], clock, skew=300)
    rep = go(fc, clock)
    assert rep.results["a"].status == "failed" and "clock" in rep.results["a"].detail
    assert not fc.apply_at


def test_unsupported_channel_blocks_preflight():
    clock = Clock()
    fc = FakeCluster(["a", "b"], clock)
    rep = go(fc, clock, change=RadioChange(channel=132, bandwidth=20))
    assert "nothing changed" in rep.reason and not fc.apply_at


def test_parse_real_shape_channels_on_config_entry():
    # Shape captured from a 4.26.1.0 hAP ac lite: tables live on each config entry.
    real = {"config": [{"iface": "wlan0", "mode": {"mode": "mesh", "channel": 131, "bandwidth": 20},
                        "bws": [5, 10, 20, 40, 80],
                        "channels": {"10": [{"label": "133 (5665)", "number": 133, "frequency": 5665}],
                                     "20": [{"label": "131 (5655)", "number": 131, "frequency": 5655}]}},
                       {"iface": "wlan1", "mode": {"mode": "wan"}}],
            "common": [{"iface": "wlan0", "mode": None, "channels": {}}]}
    st = parse_radio_state(json.dumps(real))
    assert st.channels_by_bw == {"10": [133], "20": [131]}
    assert check_change(st, RadioChange(channel=133, bandwidth=10)) is None


def test_missing_boot_hook_blocks_preflight():
    clock = Clock()
    fc = FakeCluster(["a", "b"], clock)
    fc.no_rclocal = {"b"}
    rep = go(fc, clock)
    assert rep.results["b"].status == "failed" and "rc.local" in rep.results["b"].detail
    assert not fc.apply_at
