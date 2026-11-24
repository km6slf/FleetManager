from aredn_fleet.jobs.radio import parse_radio_inventory
from aredn_fleet.rc import at_least_4267, ipq40xx_freeze_note, is_rc_or_later, ptmp_pairs
from aredn_fleet.topology import Topology
import json


def test_versions():
    assert is_rc_or_later("20261122-5e4131bf") and is_rc_or_later("20261012-e3528941")
    assert not is_rc_or_later("20260901-abcdef12") and not is_rc_or_later("4.26.10.0")
    assert is_rc_or_later("4.26.11.0") and is_rc_or_later("4.27.1.0")
    assert at_least_4267("4.26.7.0") and at_least_4267("4.26.10.0") and at_least_4267("20261027-e39b7e04")
    assert not at_least_4267("4.26.1.0") and not at_least_4267("3.25.10.0") and not at_least_4267("")


def test_ipq40xx_note_only_from_4267():
    assert ipq40xx_freeze_note("ipq40xx/mikrotik", "4.26.7.0")
    assert not ipq40xx_freeze_note("ipq40xx/mikrotik", "4.26.1.0")
    assert not ipq40xx_freeze_note("ath79/mikrotik", "4.26.7.0")


def _inv(mode):
    return parse_radio_inventory(json.dumps({"config": [{"iface": "wlan0", "mode": {"mode": mode, "channel": 149, "bandwidth": 10},
                                                         "def": {"band": "5GHz"}, "bws": [10], "channels": {}}]})).to_json()


def test_ptmp_pairs_from_inventory_and_rf_links():
    t = Topology()
    t.add_link("ap", "sta1")
    t.add_link("ap", "sta2")
    t.add_link("ap", "dtdbox", "DTD")
    t.add_link("sta1", "meshnode")
    radios = {"ap": _inv("meshap"), "sta1": _inv("meshsta"), "sta2": _inv("meshsta"),
              "dtdbox": _inv("meshsta"), "meshnode": _inv("mesh")}
    assert ptmp_pairs(t.links, radios) == [("ap", "sta1"), ("ap", "sta2")]
