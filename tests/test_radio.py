import asyncio
import json
import os
import re
import subprocess

import pytest

from aredn_fleet.jobs.radio import (BandChange, RadioConfig, build_apply_script, cluster_warnings,
                                    expectation_met, full_ssid, link_signatures, merge_changes,
                                    parse_radio_inventory, plan_node, rf_clusters,
                                    run_radio_change, validate_changes)
from aredn_fleet.topology import Topology
from aredn_fleet.transport import CommandResult


def ch(n, f):
    return {"label": f"{n} ({f})", "number": n, "frequency": f}


FIVE = {"10": [ch(131, 5655), ch(133, 5665), ch(149, 5745), ch(177, 5885)],
        "20": [ch(131, 5655), ch(149, 5745), ch(177, 5885)]}
TWO4 = {"10": [ch(-2, 2397), ch(1, 2412), ch(6, 2437)], "20": [ch(-2, 2397), ch(1, 2412), ch(6, 2437)]}


def hap_state(five_mode="mesh", five_ch=131, five_bw=20, two_mode="wan", two_ch=0, two_bw=0):
    """Shape of aredn.radios.getConfiguration() on the bench hAP ac lite (4.26.1.0)."""
    return {"config": [
        {"iface": "wlan0", "mode": {"mode": five_mode, "channel": five_ch, "bandwidth": five_bw,
                                    "ssid": "AREDN", "key": None},
         "def": {"channel": 149, "bandwidth": 10, "band": "5GHz"}, "bws": [5, 10, 20, 40, 80],
         "channels": FIVE, "managedOOB": [177]},
        {"iface": "wlan1", "mode": {"mode": two_mode, "channel": two_ch, "bandwidth": two_bw,
                                    "ssid": "4d7957694669", "key": "7769666970617373776f7264"},
         "def": {"channel": -2, "bandwidth": 10, "band": "2.4GHz"}, "bws": [10, 20],
         "channels": TWO4},
    ]}


ONE_RADIO = {"config": [{"iface": "wlan0", "mode": {"mode": "mesh", "channel": 131, "bandwidth": 20},
                         "def": {"band": "5GHz"}, "bws": [10, 20], "channels": FIVE}]}


def inv(state=None, caps="single-mesh"):
    return parse_radio_inventory(json.dumps(state or hap_state()), caps)


# --------------------------------------------------------------------------- inventory

def test_inventory_has_both_radios_with_bands_and_no_secrets():
    i = inv()
    assert [(r.index, r.iface, r.band, r.mode) for r in i.radios] == [
        (0, "wlan0", "5GHz", "mesh"), (1, "wlan1", "2.4GHz", "wan")]
    blob = json.dumps(i.to_json())
    assert "4d7957694669" not in blob and "7769666970617373776f7264" not in blob  # WAN SSID/key dropped
    assert i.multi_mesh is False
    assert inv(caps="multi-mesh").multi_mesh is True
    assert inv(caps="unknown").multi_mesh is None


def test_channel_options_match_the_node_dialog():
    r = inv().by_band("5GHz")
    # Mesh mode shows every channel at that width, with the node's own labels.
    assert [c["label"] for c in r.channel_options("mesh", 20)] == ["131 (5655)", "149 (5745)", "177 (5885)"]
    # PtMP / PtP / Station hide managedOOB channels, as the node's dialog does.
    for mode in ("meshap", "meshptp", "meshsta"):
        assert [c["number"] for c in r.channel_options(mode, 20)] == [131, 149]


# --------------------------------------------------------------------------- planning

def test_plan_channel_and_width_on_5ghz():
    p = plan_node(inv(), [BandChange("5GHz", channel=133, width=10)])
    assert p.sets == [(0, "channel", "133"), (0, "bandwidth", "10")]
    assert p.expect == [{"iface": "wlan0", "band": "5GHz", "mode": "mesh", "frequency": 5665,
                         "width": 10, "ssid": "AREDN-10-v3"}]


def test_plan_rejects_channel_not_in_dialog_list():
    with pytest.raises(ValueError, match="isn't offered at 10 MHz"):
        plan_node(inv(), [BandChange("5GHz", channel=132, width=10)])
    with pytest.raises(ValueError, match="isn't offered"):   # managedOOB in Mesh Station mode
        plan_node(inv(), [BandChange("5GHz", mode="meshsta", channel=177, width=20)])
    with pytest.raises(ValueError, match="40 MHz isn't offered"):
        plan_node(inv(ONE_RADIO), [BandChange("5GHz", width=40)])


def test_mode_change_into_mesh_resets_ssid_and_needs_multi_mesh():
    change = [BandChange("2.4GHz", mode="mesh", channel=-2, width=10)]
    with pytest.raises(ValueError, match="two radios"):
        plan_node(inv(), change)                       # 5 GHz is already mesh, firmware is single-mesh
    p = plan_node(inv(caps="multi-mesh"), change)
    assert (1, "mode", "mesh") in p.sets and (1, "ssid", "AREDN") in p.sets
    assert (1, "channel", "-2") in p.sets and (1, "bandwidth", "10") in p.sets


def test_moving_mesh_between_radios_is_allowed_on_single_mesh_firmware():
    p = plan_node(inv(), [BandChange("5GHz", mode="off"),
                          BandChange("2.4GHz", mode="mesh", channel=1, width=20)])
    assert (0, "mode", "off") in p.sets and (1, "mode", "mesh") in p.sets


def test_wan_radio_is_never_touched_by_channel_changes():
    with pytest.raises(ValueError, match="WAN Client"):
        plan_node(inv(), [BandChange("2.4GHz", channel=6)])


def test_missing_band_is_skipped_not_failed():
    p = plan_node(inv(ONE_RADIO), [BandChange("2.4GHz", channel=1), BandChange("5GHz", channel=149)])
    assert p.sets == [(0, "channel", "149")] and "no 2.4 GHz radio" in p.notes[0]


def test_change_validation():
    with pytest.raises(ValueError):
        BandChange("5GHz", mode="lan").validate()
    with pytest.raises(ValueError):
        BandChange("5GHz", mode="meshptp").validate()
    with pytest.raises(ValueError):
        BandChange("5GHz").validate()
    with pytest.raises(ValueError):
        validate_changes([BandChange("5GHz", channel=1), BandChange("5GHz", width=10)])
    BandChange("5GHz", mode="meshptp", peer="02:00:00:00:00:01").validate()


# --------------------------------------------------------------------------- script + live checks

@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX sh")
def test_script_is_valid_shell_and_sets_both_radios():
    s = build_apply_script([(0, "channel", "133"), (1, "mode", "off")], 2_000_000_000, 600)
    subprocess.run(["sh", "-n"], input=s, text=True, check=True)
    assert 'S("radio0_channel", "133")' in s and 'S("radio1_mode", "off")' in s
    assert "c.setSettingAsString || c.setSetting" in s
    assert 'c.setSettingAsList("radio0_channels", [])' in s
    assert "/etc/aredn-fleet-radio" in s and "rc.local" in s


def test_script_refuses_unsafe_values():
    with pytest.raises(ValueError):
        build_apply_script([(0, "ssid", "x\"; reboot; \"")], 0, 600)


IW_MESH = """Interface wlan0
	type IBSS
	channel 133 (5665 MHz), width: 20 MHz, center1: 5665 MHz"""


def test_expectation_uses_frequency_and_type():
    exp = {"iface": "wlan0", "band": "5GHz", "mode": "mesh", "frequency": 5665, "width": 10}
    assert expectation_met(exp, IW_MESH)                       # 10 MHz shows as 20 in iw: tolerated
    assert not expectation_met({**exp, "frequency": 5655}, IW_MESH)
    assert not expectation_met({**exp, "mode": "meshsta"}, IW_MESH)
    assert expectation_met({"iface": "wlan1", "band": "2.4GHz", "mode": "off"}, "command failed: No such device")


def test_rf_clusters_split_on_dtd():
    t = Topology()
    t.add_link("a", "b", "RF")
    t.add_link("b", "c", "DTD")
    t.add_link("c", "d", "RF")
    assert sorted(map(sorted, rf_clusters(t, {"a", "b", "c", "d"}))) == [["a", "b"], ["c", "d"]]
    assert cluster_warnings(t, {"a"}) and "b" in cluster_warnings(t, {"a"})[0]


# --------------------------------------------------------------------------- whole job, simulated

class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += max(s, 1)


FREQ = {int(c["number"]): c["frequency"] for c in FIVE["10"] + FIVE["20"]}


class FakeCluster:
    """Nodes that each have the bench hAP's two radios and RF links to each other."""

    def __init__(self, nodes, clock, broken=(), skew=0, caps="single-mesh"):
        self.clock, self.broken, self.skew, self.caps = clock, set(broken), skew, caps
        self.nodes = list(nodes)
        self.ch = {n: 131 for n in nodes}
        self.apply_at, self.pending, self.scripts = {}, {}, {}
        self.confirmed, self.no_rclocal = set(), set()

    def _tick(self, n):
        if n in self.apply_at and self.clock.now() >= self.apply_at[n] and n not in self.broken:
            self.ch[n] = self.pending[n]

    async def sysinfo(self, n):
        for x in self.nodes:
            self._tick(x)
        nb = [x for x in self.nodes if x != n and self.ch[x] == self.ch[n]]
        return {"link_info": {str(i): {"hostname": f"{x}.local.mesh", "linkType": "RF"}
                              for i, x in enumerate(nb)}}

    async def connect(self, n):
        fc = self

        class S:
            async def run(self, cmd, timeout=60):
                fc._tick(n)
                if cmd.startswith("ucode"):
                    return CommandResult(0, json.dumps(hap_state(five_ch=fc.ch[n])), "")
                if "radio-and-antenna" in cmd:
                    return CommandResult(0, fc.caps + "\n", "")
                if cmd.startswith("[ -f /etc/rc.local"):
                    return CommandResult(0, "" if n in fc.no_rclocal else "ok\n", "")
                if cmd.startswith("date"):
                    return CommandResult(0, str(int(fc.clock.now() + fc.skew)), "")
                if "trap '' HUP" in cmd and "sh /" in cmd:
                    s = fc.scripts[n]
                    fc.apply_at[n] = int(re.search(r"\[ (\d+) -gt", s).group(1))
                    fc.pending[n] = int(re.search(r'radio0_channel", "(-?\d+)"', s).group(1))
                if cmd.startswith("iw dev wlan0"):
                    c = fc.ch[n]
                    return CommandResult(0, f"type IBSS\nchannel {c} ({FREQ[c]} MHz), width: 20 MHz", "")
                if "/confirmed" in cmd:
                    fc.confirmed.add(n)
                return CommandResult(0, "", "")

            async def upload(self, local, remote):
                fc.scripts[n] = local.read_text()

            async def close(self):
                pass
        return S()


CFG = RadioConfig(lead_seconds=30, revert_after=600, poll_interval=10)


def go(fc, clock, execute=True, changes=(BandChange("5GHz", channel=133, width=10),)):
    ch = changes if isinstance(changes, dict) else list(changes)
    return asyncio.run(run_radio_change(sorted(fc.nodes), ch, fc.connect, fc.sysinfo,
                                        CFG, execute=execute, sleep=clock.sleep, now=clock.now))


def test_dry_run_reads_only():
    clock = Clock()
    fc = FakeCluster(["a", "b"], clock)
    rep = go(fc, clock, execute=False)
    assert {r.status for r in rep.results.values()} == {"would-change"}
    assert "5 GHz Mesh ch 131 (5655) @ 20 MHz" in rep.results["a"].before
    assert "2.4 GHz WAN Client" in rep.results["a"].before
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


def test_preflight_blocks_skew_missing_hook_and_bad_channel():
    clock = Clock()
    fc = FakeCluster(["a"], clock, skew=300)
    assert "clock" in go(fc, clock).results["a"].detail and not fc.apply_at
    fc = FakeCluster(["a", "b"], clock)
    fc.no_rclocal = {"b"}
    assert "rc.local" in go(fc, clock).results["b"].detail and not fc.apply_at
    fc = FakeCluster(["a", "b"], clock)
    rep = go(fc, clock, changes=[BandChange("5GHz", channel=133, width=20)])
    assert "nothing changed" in rep.reason and not fc.apply_at


def test_band_names_and_6ghz_inference():
    from aredn_fleet.jobs.radio import band_label, band_of
    assert [band_label(b) for b in ("900MHz", "2.4GHz", "3GHz", "5GHz", "6GHz")] == [
        "900 MHz", "2.4 GHz", "3.4 GHz", "5 GHz", "6 GHz"]
    assert band_of({"channels": {"20": [{"number": 37, "frequency": 6135}]}}) == "6GHz"
    assert band_of({"channels": {"20": [{"number": 149, "frequency": 5745}]}}) == "5GHz"
    assert band_of({"channels": {"10": [{"number": 76, "frequency": 3380}]}}) == "3.4GHz"


def test_two_radios_on_one_band_are_refused():
    two5 = {"config": [ONE_RADIO["config"][0], {**ONE_RADIO["config"][0], "iface": "wlan1"}]}
    with pytest.raises(ValueError, match="2 radios on 5 GHz"):
        plan_node(inv(two5, caps="multi-mesh"), [BandChange("5GHz", channel=149)])



# --------------------------------------------------------------------------- SSIDs and node-specific settings

def test_full_ssid_rules_match_node_setup():
    assert full_ssid("AREDN", "mesh", 131, 10, "5GHz") == "AREDN-10-v3"
    assert full_ssid("IDEC", "mesh", 131, 10, "5GHz") == "IDEC-10-v3"
    assert full_ssid("AREDN", "mesh", 3, 2, "900MHz") == "AREDN-3-2-v3"       # HaLow adds the channel
    assert full_ssid("AREDN", "meshsta", 149, 10, "5GHz") == "AREDN-149-10-v3"


def test_mesh_ssid_kept_wan_ssid_dropped():
    i = inv()
    assert i.by_band("5GHz").ssid == "AREDN" and i.by_band("2.4GHz").ssid is None


def test_custom_ssid_is_set_and_verified():
    p = plan_node(inv(), [BandChange("5GHz", ssid="IDEC")])
    assert p.sets == [(0, "ssid", "IDEC")]
    assert p.expect[0]["ssid"] == "IDEC-20-v3"
    exp = p.expect[0]
    iw = "type IBSS\n\tssid AREDN-20-v3\n\tchannel 131 (5655 MHz), width: 20 MHz"
    assert not expectation_met(exp, iw)
    assert expectation_met(exp, iw.replace("AREDN-20", "IDEC-20"))


def test_unknown_current_ssid_is_never_reset_to_aredn():
    st = hap_state()
    del st["config"][0]["mode"]["ssid"]                 # couldn't read it
    p = plan_node(inv(st), [BandChange("5GHz", channel=149)])
    assert not any(k == "ssid" for _, k, _ in p.sets)


def test_entering_mesh_from_wan_uses_aredn_or_requested_ssid():
    p = plan_node(inv(caps="multi-mesh"), [BandChange("2.4GHz", mode="mesh", channel=1, width=10)])
    assert (1, "ssid", "AREDN") in p.sets
    p = plan_node(inv(caps="multi-mesh"), [BandChange("2.4GHz", mode="mesh", channel=1, width=10, ssid="IDEC")])
    assert (1, "ssid", "IDEC") in p.sets and (1, "ssid", "AREDN") not in p.sets


def test_ssid_validation():
    for bad in ["", "has space", "x" * 21, "semi;colon"]:
        with pytest.raises(ValueError):
            BandChange("5GHz", ssid=bad).validate()
    BandChange("5GHz", ssid="IDEC-Net_2").validate()


def test_own_settings_replace_the_group_on_that_band_blanks_keep_current():
    group = [BandChange("5GHz", channel=133, width=20, ssid="IDEC"), BandChange("2.4GHz", channel=6)]
    merged = merge_changes(group, {"5GHz": {"channel": 149}})
    by = {c.band: c for c in merged}
    # 5 GHz: only the node's own channel; width and SSID are NOT taken from the group
    assert by["5GHz"] == BandChange("5GHz", channel=149)
    # a band without own settings still follows the group
    assert by["2.4GHz"] == BandChange("2.4GHz", channel=6)
    assert merge_changes(group, None) == group
    assert merge_changes([], {}) == []
    assert merge_changes([], {"2.4GHz": {"mode": "off"}}) == [BandChange("2.4GHz", mode="off")]


def test_blank_own_fields_keep_the_nodes_current_values_in_the_plan():
    group = [BandChange("5GHz", channel=149, width=20, ssid="IDEC")]
    merged = merge_changes(group, {"5GHz": {"channel": 133}})   # width/SSID left blank
    p = plan_node(inv(hap_state(five_ch=131, five_bw=10)), merged)
    assert p.sets == [(0, "channel", "133")]                     # width stays 10, SSID stays AREDN
    assert p.final[0] == ("mesh", 133, 10, "AREDN")


def test_link_signatures_follow_channel_and_ssid():
    a = inv()
    pa = plan_node(a, [BandChange("5GHz", channel=133, width=10)])
    pb = plan_node(inv(), [BandChange("5GHz", channel=133, width=10)])
    pc = plan_node(inv(), [BandChange("5GHz", channel=149, width=10)])
    pd = plan_node(inv(), [BandChange("5GHz", channel=133, width=10, ssid="IDEC")])
    assert link_signatures(a, pa) & link_signatures(a, pb)
    assert not link_signatures(a, pa) & link_signatures(a, pc)      # different channel
    assert not link_signatures(a, pa) & link_signatures(a, pd)      # different SSID


def test_node_with_own_channel_stays_in_group_and_split_is_expected():
    clock = Clock()
    fc = FakeCluster(["a", "b", "c"], clock)
    group = [BandChange("5GHz", channel=133, width=10)]
    per_node = {"a": group, "b": group,
                "c": merge_changes(group, {"5GHz": {"channel": 149}})}   # c has its own channel
    dry = go(fc, clock, execute=False, changes=per_node)
    assert "WILL LOSE RF LINK TO a, b" in dry.results["c"].detail
    rep = go(fc, clock, changes=per_node)
    assert rep.confirmed, rep.reason                    # the intended split doesn't cause a revert
    assert fc.ch == {"a": 133, "b": 133, "c": 149}


def test_only_node_settings_no_group_change():
    clock = Clock()
    fc = FakeCluster(["a", "b"], clock)
    rep = go(fc, clock, execute=False, changes={"a": [], "b": [BandChange("5GHz", channel=149)]})
    assert rep.results["a"].status == "unchanged" and rep.results["b"].status == "would-change"
    with pytest.raises(ValueError, match="nothing to change"):
        go(fc, clock, execute=False, changes={"a": [], "b": []})


# Nightly/RC getConfiguration(): one channel list with widths and per-width limits
# (shape from the bench hAP ac lite on 20261122-5e4131bf).
RC_FIVE = [
    {"number": 36, "frequency": 5180, "label": "36 (5180)", "widths": [5, 10, 20, 40],
     "unlicensed": {"20": {"maxEIRP": 100}, "40": {"maxEIRP": 100}}, "amateur": {}},
    {"number": 60, "frequency": 5300, "label": "60 (5300)", "widths": [5, 10, 20],
     "unlicensed": {"20": {"maxEIRP": 100, "DFS": True}}, "amateur": {}},
    {"number": 131, "frequency": 5655, "label": "131 (5655)", "widths": [5, 10, 20],
     "unlicensed": {}, "amateur": {"20": {"maxPEP": 30}}},
    {"number": 132, "frequency": 5660, "label": "132 (5660)", "widths": [5, 10, 20, 40, 80],
     "unlicensed": {"20": {"maxEIRP": 100}}, "amateur": {}},
]


def rc_state():
    return {"config": [
        {"iface": "wlan0", "mode": {"mode": "mesh", "channel": 131, "channels": None, "bandwidth": 20},
         "def": {"channel": 149, "bandwidth": 10, "band": "5GHz"}, "bws": [5, 10, 20, 40, 80],
         "channels": RC_FIVE},
        {"iface": "wlan1", "mode": {"mode": "wan", "channel": None, "bandwidth": None},
         "def": {"channel": -2, "bandwidth": 10, "band": "2.4GHz"}, "bws": [10, 20],
         "channels": [{"number": 1, "frequency": 2412, "label": "1 (2412)", "widths": [5, 10, 20],
                       "unlicensed": {}, "amateur": {}}]},
    ]}


def test_rc_channel_list_format_is_grouped_by_width_like_the_dialog():
    i = parse_radio_inventory(json.dumps(rc_state()), "multi-mesh")
    r = i.radios[0]
    assert r.band == "5GHz" and r.channel == 131 and r.bandwidth == 20
    assert [c["number"] for c in r.channel_options("mesh", 20)] == [36, 60, 131, 132]
    assert [c["number"] for c in r.channel_options("meshsta", 40)] == [36, 132]
    assert [c["number"] for c in r.channel_options("mesh", 80)] == [132]
    assert r.label(131, 20) == "131 (5655) [ham 30 dBm]"
    assert r.frequency(132, 80) == 5660
    assert i.radios[1].channel_options("mesh", 20)[0]["number"] == 1
    # with unlicensed channels enabled, the dialog adds Part 15 limits
    u = parse_radio_inventory(json.dumps(rc_state()), "multi-mesh", unlicensed=True).radios[0]
    assert u.label(60, 20) == "60 (5300) [dfs, 100 dB]"
    assert u.label(131, 20) == "131 (5655) [ham 30 dBm]"


def test_supported_modes_come_from_the_node():
    s = rc_state()
    s["modes"] = [{"mesh": True, "meshap": False, "meshptp": True, "meshsta": True, "lan": True, "wan": True},
                  {"mesh": False, "meshap": False, "meshptp": False, "meshsta": False, "lan": False, "wan": False}]
    i = parse_radio_inventory(json.dumps(s), "multi-mesh")
    assert i.radios[0].modes == ["lan", "mesh", "meshptp", "meshsta", "wan"]
    assert i.radios[1].modes is None          # all false: the firmware didn't answer
    with pytest.raises(ValueError, match="doesn't support Mesh PtMP"):
        plan_node(i, [BandChange("5GHz", mode="meshap", channel=131, width=20)])
    from aredn_fleet.jobs.radio import NodeRadios
    assert NodeRadios.from_json(i.to_json()).radios[0].modes == i.radios[0].modes


def test_3_25_format_and_tiny_build_purposes():
    """3.25.x getConfiguration(): one plain channel list (5/10/20 MHz) plus channels40/80, no
    supportsFeature; without hostapd its dialog offers only Off and Mesh (tiny builds)."""
    import json as _json
    from aredn_fleet.jobs.radio import parse_radio_inventory
    ch = [{"number": -2, "label": "-2 (2397)", "frequency": 2397},
          {"number": 1, "label": "1 (2412)", "frequency": 2412}]
    cfg = {"iface": "wlan0", "def": {"band": "2.4GHz"}, "bws": [5, 10, 20], "channels": ch,
           "channels40": [], "channels80": [], "managedOOB": [-4, -3, -2, -1, 0],
           "mode": {"mode": "mesh", "channel": -2, "bandwidth": 10, "ssid": "AREDN"}}
    raw = _json.dumps({"config": [cfg], "modes": [None], "hostapd": False})
    (r,) = parse_radio_inventory(raw).radios
    assert [c["number"] for c in r.channels["10"]] == [-2, 1]
    assert r.channels["20"] == r.channels["5"] == r.channels["10"]
    assert r.label(-2, 10) == "-2 (2397)"
    assert r.modes == ["mesh"]
    # with hostapd and no supportsFeature: nothing ruled out
    raw = _json.dumps({"config": [cfg], "modes": [None], "hostapd": True})
    assert parse_radio_inventory(raw).radios[0].modes is None
