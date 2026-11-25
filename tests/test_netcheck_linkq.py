"""Stale mesh routes on the PC, neighbours' view of a link, and retrying lossy steps."""
import asyncio

import pytest

from aredn_fleet.linkq import describe_tracker, link_report
from aredn_fleet.netcheck import stale_mesh_routes
from aredn_fleet.transport import AuthFailed, with_retries

# The bench PC after moving its cable from the NanoStation to the hAP (real addresses from it).
ROUTES = [
    {"prefix": "0.0.0.0/0", "next_hop": "192.168.1.1", "iface": "Wi-Fi", "ifindex": 7},
    {"prefix": "10.0.0.0/8", "next_hop": "10.136.250.61", "iface": "Ethernet", "ifindex": 12},
    {"prefix": "0.0.0.0/0", "next_hop": "10.99.154.177", "iface": "Ethernet", "ifindex": 12},
    {"prefix": "10.0.0.0/8", "next_hop": "10.99.154.177", "iface": "Ethernet", "ifindex": 12},
    {"prefix": "10.99.154.176/29", "next_hop": "0.0.0.0", "iface": "Ethernet", "ifindex": 12},
]
ADDRS = [{"ip": "10.99.154.182", "prefix_len": 29, "ifindex": 12},
         {"ip": "192.168.1.115", "prefix_len": 24, "ifindex": 7}]


def test_leftover_mesh_route_is_found_with_its_removal_command():
    (s,) = stale_mesh_routes(ROUTES, ADDRS)
    assert s["next_hop"] == "10.136.250.61" and s["iface"] == "Ethernet"
    assert s["command"] == "route delete 10.0.0.0 mask 255.0.0.0 10.136.250.61"


def test_good_routes_and_other_networks_are_left_alone():
    assert stale_mesh_routes([r for r in ROUTES if r["next_hop"] != "10.136.250.61"], ADDRS) == []
    # a 192.168 route through an unreachable hop isn't a mesh route: not ours to judge
    assert stale_mesh_routes([{"prefix": "192.168.5.0/24", "next_hop": "10.9.9.9",
                               "iface": "Ethernet", "ifindex": 12}], ADDRS) == []


def test_neighbours_report_the_link_like_their_own_page():
    si = {"lqm": {"info": {"trackers": {"aa": {"hostname": "KM6SLF-750M-217", "type": "RF",
                                                "quality": 28, "snr": 37, "rtt": 15}}}}}
    lines = link_report("km6slf-750m-217", ["km6slf-test-hap-86"], lambda nb: si)
    assert lines == ["km6slf-test-hap-86 sees its link to km6slf-750m-217: "
                     "RF link: 72% errors, SNR 37, 15 ms round trip"]
    gone = link_report("km6slf-750m-217", ["km6slf-test-hap-86"],
                       lambda nb: {"lqm": {"info": {"trackers": {}}}})
    assert "doesn't list" in gone[0]
    assert describe_tracker({}) == "no measurements yet"


def test_transient_failures_are_retried_but_wrong_credentials_are_not():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise asyncio.TimeoutError()
        return "ok"

    async def nosleep(_):
        return None

    assert asyncio.run(with_retries(flaky, sleep=nosleep)) == "ok" and len(calls) == 3

    async def denied():
        calls.append(2)
        raise AuthFailed("no")

    calls.clear()
    with pytest.raises(AuthFailed):
        asyncio.run(with_retries(denied, sleep=nosleep))
    assert calls == [2]
