"""Spot routes on this PC that send mesh traffic nowhere. Read-only: never changes a route.

Seen on the bench: after the PC's cable moved from one node to another, Windows kept the old
node's route for the whole mesh (10.0.0.0/8 via the old node's LAN address) next to the new one,
with the same metric. That old next hop isn't on any network the PC is connected to, so Windows
sometimes picked it and the connection went nowhere ("Destination host unreachable" from the
PC's own address). Fleet Manager reports such routes with the command that removes them; deleting
a route needs administrator rights and is the user's call.
"""
from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from typing import Any

MESH_NET = ipaddress.ip_network("10.0.0.0/8")


def stale_mesh_routes(routes: list[dict[str, Any]], addrs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Routes into the mesh (inside 10.0.0.0/8) whose next hop isn't on any network this PC has
    an address on, on that interface.

    ``routes``: ``{prefix, next_hop, iface, ifindex}``; ``addrs``: ``{ip, prefix_len, ifindex}``.
    """
    nets: dict[Any, list] = {}
    for a in addrs:
        try:
            nets.setdefault(a.get("ifindex"), []).append(
                ipaddress.ip_interface(f"{a['ip']}/{a['prefix_len']}").network)
        except (KeyError, ValueError):
            continue
    out = []
    for r in routes:
        try:
            dest = ipaddress.ip_network(str(r.get("prefix")), strict=False)
            hop = ipaddress.ip_address(str(r.get("next_hop")))
        except ValueError:
            continue
        if dest.version != 4 or not dest.subnet_of(MESH_NET) or hop.is_unspecified:
            continue
        if any(hop in n for n in nets.get(r.get("ifindex"), [])):
            continue
        out.append({
            "prefix": str(dest), "next_hop": str(hop), "iface": r.get("iface", ""),
            "command": f"route delete {dest.network_address} mask {dest.netmask} {hop}",
        })
    return out


_PS = ("$r = Get-NetRoute -AddressFamily IPv4 | Select-Object DestinationPrefix, NextHop, "
       "InterfaceAlias, InterfaceIndex; "
       "$a = Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress, PrefixLength, "
       "InterfaceIndex; "
       "@{ routes = @($r); addrs = @($a) } | ConvertTo-Json -Depth 3 -Compress")


def check() -> dict[str, Any]:
    """This PC's stale mesh routes. Windows only for now; elsewhere ``supported`` is False."""
    if sys.platform != "win32":
        return {"supported": False, "stale": []}
    try:
        out = subprocess.run(  # noqa: S603,S607
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        data = json.loads(out or "{}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"supported": True, "stale": [], "error": str(exc)}

    def as_list(v):
        return v if isinstance(v, list) else [v] if isinstance(v, dict) else []

    routes = [{"prefix": r.get("DestinationPrefix"), "next_hop": r.get("NextHop"),
               "iface": r.get("InterfaceAlias"), "ifindex": r.get("InterfaceIndex")}
              for r in as_list(data.get("routes"))]
    addrs = [{"ip": a.get("IPAddress"), "prefix_len": a.get("PrefixLength"),
              "ifindex": a.get("InterfaceIndex")} for a in as_list(data.get("addrs"))]
    return {"supported": True, "stale": stale_mesh_routes(routes, addrs)}
