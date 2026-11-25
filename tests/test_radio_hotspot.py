"""LAN Hotspot and WAN Client purposes: written as the node's own Radios & Antennas page does
(hex-encoded radioN_ssid / radioN_key, radioN_encryption), checked against the node's own channel
list, verified live, and never leaking a password or a WAN network name."""
import json

import pytest

from aredn_fleet.jobs.radio import (BandChange, build_apply_script, expectation_met, hexenc,
                                    hotspot_table, ledger_for, merge_changes, parse_radio_inventory,
                                    plan_node, safe_sets)
from test_radio import hap_state, inv

PW = "correct-horse-9"


def test_reading_keeps_a_hotspot_name_but_never_passwords_or_wan_names():
    lan = hap_state(two_mode="lan", two_ch=6, two_bw=20)
    lan["config"][1]["mode"].update(ssid=hexenc("AREDN-LAN"), encryption="psk2")
    r = parse_radio_inventory(json.dumps(lan)).radios[1]
    assert (r.mode, r.ap_ssid, r.encryption) == ("lan", "AREDN-LAN", "psk2")
    wan = inv().radios[1]
    assert wan.mode == "wan" and wan.ap_ssid is None and "key" not in json.dumps(wan.__dict__)


def test_mesh_radio_becomes_a_protected_hotspot():
    p = plan_node(inv(), [BandChange("5GHz", mode="lan", channel=149, ap_ssid="Club Hotspot",
                                     encryption="psk2", key=PW)])
    assert p.sets == [(0, "mode", "lan"), (0, "channel", "149"),
                      (0, "ssid", hexenc("Club Hotspot")), (0, "encryption", "psk2"),
                      (0, "key", hexenc(PW))]
    assert p.expect == [{"iface": "wlan0", "band": "5GHz", "mode": "lan", "frequency": 5745,
                         "ssid": "Club Hotspot"}]
    shown = json.dumps(safe_sets(p))
    assert PW not in shown and hexenc(PW) not in shown and "Club Hotspot" in shown
    row = ledger_for(inv(), p)[0]
    assert row["after"]["mode"] == "LAN Hotspot" and row["after"]["security"] == "WPA2 PSK"
    assert row["after"]["password"] == "changed (not shown)"


def test_hotspot_needs_name_channel_security_and_password():
    for change, msg in [
        (BandChange("5GHz", mode="lan", ap_ssid="H", encryption="psk2", key=PW), "channel"),
        (BandChange("5GHz", mode="lan", channel=149, encryption="psk2", key=PW), "network name"),
        (BandChange("5GHz", mode="lan", channel=149, ap_ssid="H", key=PW), "security"),
        (BandChange("5GHz", mode="lan", channel=149, ap_ssid="H", encryption="psk2"), "password"),
        (BandChange("5GHz", mode="lan", channel=133, ap_ssid="H", encryption="none"), "isn't offered"),
    ]:
        with pytest.raises(ValueError, match=msg):
            plan_node(inv(), [change])
    # an open hotspot needs no password
    plan_node(inv(), [BandChange("5GHz", mode="lan", channel=149, ap_ssid="H", encryption="none")])


def test_existing_hotspot_keeps_its_password_when_only_the_channel_moves():
    lan = hap_state(two_mode="lan", two_ch=6, two_bw=20)
    lan["config"][1]["mode"].update(ssid=hexenc("AREDN-LAN"), encryption="psk2")
    p = plan_node(parse_radio_inventory(json.dumps(lan)), [BandChange("2.4GHz", channel=1)])
    assert p.sets == [(1, "channel", "1")]


def test_wan_client_joins_a_network_and_is_verified_by_association():
    st = hap_state(two_mode="mesh", two_ch=-2, two_bw=10)
    p = plan_node(inv(st, "multi-mesh"), [BandChange("2.4GHz", mode="wan", ap_ssid="HomeNet", key=PW)])
    assert p.sets == [(1, "mode", "wan"), (1, "ssid", hexenc("HomeNet")), (1, "key", hexenc(PW))]
    exp = p.expect[0]
    assert expectation_met(exp, "Interface wlan1\n\ttype managed\n\tssid HomeNet\n")
    assert not expectation_met(exp, "Interface wlan1\n\ttype managed\n")        # not joined
    assert not expectation_met(exp, "Interface wlan1\n\ttype managed\n\tssid Other\n")
    shown = json.dumps(safe_sets(p))
    assert "HomeNet" not in shown and hexenc("HomeNet") not in shown and PW not in shown
    # open network: an empty password is written as empty
    p = plan_node(inv(st, "multi-mesh"), [BandChange("2.4GHz", mode="wan", ap_ssid="Open", key="")])
    assert (1, "key", "") in p.sets


def test_wan_rules():
    st = hap_state(two_mode="mesh", two_ch=-2, two_bw=10)
    with pytest.raises(ValueError, match="network name"):
        plan_node(inv(st, "multi-mesh"), [BandChange("2.4GHz", mode="wan")])
    with pytest.raises(ValueError, match="password"):
        plan_node(inv(st, "multi-mesh"), [BandChange("2.4GHz", mode="wan", ap_ssid="HomeNet")])
    with pytest.raises(ValueError, match="only one radio"):     # 2.4 is already the WAN Client
        plan_node(inv(), [BandChange("5GHz", mode="wan", ap_ssid="X", key=PW)])
    with pytest.raises(ValueError, match="follows its access point"):
        plan_node(inv(), [BandChange("2.4GHz", channel=6)])


def test_redacted_change_and_script_never_show_the_password():
    c = BandChange("2.4GHz", mode="wan", ap_ssid="HomeNet", key=PW)
    assert PW not in json.dumps(c.redacted()) and "HomeNet" not in json.dumps(c.redacted())
    assert PW not in repr(c)
    script = build_apply_script([(1, "mode", "wan"), (1, "ssid", hexenc("HomeNet")),
                                 (1, "key", "")], 2_000_000_000, 600)
    assert 'S("radio1_key", "");' in script
    with pytest.raises(ValueError):
        build_apply_script([(1, "ssid", "")], 2_000_000_000, 600)    # only a password may be empty


def test_own_settings_carry_the_password_into_the_merge():
    own = {"2.4GHz": {"mode": "lan", "channel": 6, "ap_ssid": "Shelter", "encryption": "psk2",
                      "key": PW}}
    (c,) = merge_changes([BandChange("2.4GHz", channel=1)], own)
    assert (c.mode, c.channel, c.ap_ssid, c.encryption, c.key) == ("lan", 6, "Shelter", "psk2", PW)


def test_hotspot_channels_follow_each_firmware_s_dialog():
    nightly = {"channels": [
        {"number": -2, "label": "-2 (2397)", "frequency": 2397, "widths": [10], "unlicensed": {}},
        {"number": 1, "label": "1 (2412)", "frequency": 2412, "widths": [20],
         "unlicensed": {"20": {}}},
        {"number": 52, "label": "52 (5260)", "frequency": 5260, "widths": [20],
         "unlicensed": {"20": {"DFS": True, "NO_OUTDOOR": True}}},
        {"number": 131, "label": "131 (5655)", "frequency": 5655, "widths": [20], "unlicensed": {}},
    ]}
    assert [(c["number"], c["label"]) for c in hotspot_table(nightly)] == [
        (1, "1"), (52, "52 [dfs, indoor]")]
    r426 = {"channels": {"10": [{"number": -2, "frequency": 2397}],
                         "20": [{"number": 1, "label": "1 (2412)", "frequency": 2412}]}}
    assert [(c["number"], c["label"]) for c in hotspot_table(r426)] == [(1, "1")]
    r325 = {"channels": [{"number": -2, "label": "-2 (2397)", "frequency": 2397},
                         {"number": 1, "label": "1 (2412)", "frequency": 2412}]}
    assert [c["number"] for c in hotspot_table(r325)] == [-2, 1]


def test_own_settings_password_lives_in_the_credential_store_not_the_file(tmp_path):
    from aredn_fleet.web.state import AppState
    st = AppState(fw_dir=tmp_path / "fw", config_dir=tmp_path / "cfg")
    st.wifi_keys = {}
    st.set_override("n1", {"2.4GHz": {"mode": "lan", "channel": 6, "ap_ssid": "Shelter",
                                      "encryption": "psk2", "key": PW}}, "EOC hotspot")
    on_disk = (tmp_path / "cfg" / "overrides.json").read_text()
    assert PW not in on_disk and '"key_saved": true' in on_disk
    assert st.own_radio("n1")["2.4GHz"]["key"] == PW
    # editing without retyping the password keeps it
    st.set_override("n1", {"2.4GHz": {"mode": "lan", "channel": 1, "ap_ssid": "Shelter",
                                      "encryption": "psk2"}})
    assert st.own_radio("n1")["2.4GHz"] == {"mode": "lan", "channel": 1, "ap_ssid": "Shelter",
                                            "encryption": "psk2", "key": PW}
    # an open WAN network is remembered as "no password", and removal forgets the saved one
    st.set_override("n1", {"2.4GHz": {"mode": "wan", "ap_ssid": "Cafe", "key": ""}})
    assert st.own_radio("n1")["2.4GHz"]["key"] == "" and st.wifi_keys == {}
    st.delete_override("n1")
    assert st.own_radio("n1") is None
