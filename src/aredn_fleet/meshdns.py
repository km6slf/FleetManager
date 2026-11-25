"""Look up mesh names on the mesh itself, inside Fleet Manager only.

On a PC that is also on Wi-Fi (or a VPN), Windows may send ``*.local.mesh`` lookups to the home
router first; those stall or fail before Windows tries the mesh node. Fleet Manager therefore asks
the node it reaches the mesh through (its dnsmasq answers for every mesh name, the same way the
node's own pages find other nodes) and connects to the address it gets back.

Nothing on the PC changes: the system's DNS settings are untouched, and only names ending in
``.local.mesh`` (or bare node names) are looked up this way. If the mesh server isn't known yet or
doesn't answer, the system's normal lookup is used.
"""
from __future__ import annotations

import ipaddress
import os
import random
import socket
import struct
import threading
import time

SUFFIX = ".local.mesh"
TTL = 300.0           # seconds a looked-up address is reused
TIMEOUT = 2.0         # per try
TRIES = 2
PORT = 53            # DNS port on the mesh node (tests use another)

_lock = threading.Lock()
_server: str | None = os.environ.get("AREDN_FLEET_DNS") or None
_cache: dict[str, tuple[str, float]] = {}


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def mesh_name(host: str) -> str | None:
    """``node`` or ``node.local.mesh`` -> ``node.local.mesh``; other names and addresses -> None."""
    h = host.strip().rstrip(".").lower()
    if not h or is_ip(h) or h in ("localnode", "localnode" + SUFFIX):
        # "localnode" means whichever node this PC is cabled to now: always the system's answer
        # (a remembered mesh server could be the node it was cabled to before).
        return None
    if h.endswith(SUFFIX):
        return h
    return None if "." in h else h + SUFFIX


def server() -> str | None:
    return _server


def set_server(ip: str | None) -> None:
    """Use this address (the node the PC is cabled to) for mesh lookups."""
    global _server
    if ip and not is_ip(ip):
        return
    with _lock:
        if ip != _server:
            _cache.clear()
        _server = ip


def learn_from(sysinfo: dict, fallback_ip: str | None = None) -> None:
    """Pick the mesh DNS server from the first node's status: its LAN address (``br-lan``),
    else the address the first lookup gave. An ``AREDN_FLEET_DNS`` setting always wins."""
    if os.environ.get("AREDN_FLEET_DNS"):
        return
    for i in sysinfo.get("interfaces") or []:
        if isinstance(i, dict) and i.get("name") == "br-lan" and is_ip(str(i.get("ip") or "")):
            set_server(str(i["ip"]))
            return
    if fallback_ip and is_ip(fallback_ip):
        set_server(fallback_ip)


def _query(name: str, server_ip: str, timeout: float = TIMEOUT) -> str | None:
    """One DNS A query over UDP. Returns the first IPv4 address, or None."""
    qid = random.randrange(0x10000)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for part in name.split("."):
        q += bytes([len(part)]) + part.encode("ascii")
    q += b"\x00" + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q, (server_ip, PORT))
        while True:
            data, _ = s.recvfrom(1500)
            if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid:
                break
    flags, qd, an = struct.unpack(">HHH", data[2:8])
    if flags & 0x000F or not an:
        return None                       # NXDOMAIN etc., or no answer
    pos = 12
    for _ in range(qd):                   # skip the question
        pos = _skip_name(data, pos) + 4
    for _ in range(an):
        pos = _skip_name(data, pos)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:
            return socket.inet_ntoa(data[pos:pos + 4])
        pos += rdlen
    return None


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        n = data[pos]
        if n == 0:
            return pos + 1
        if n & 0xC0 == 0xC0:              # compression pointer
            return pos + 2
        pos += n + 1


def lookup(host: str) -> str | None:
    """The mesh address for a mesh name, asked of the mesh node; None if not a mesh name,
    no mesh server is known, or it didn't answer."""
    name = mesh_name(host)
    srv = _server
    if not name or not srv:
        return None
    now = time.monotonic()
    with _lock:
        hit = _cache.get(name)
        if hit and hit[1] > now:
            return hit[0]
    for _ in range(TRIES):
        try:
            ip = _query(name, srv)
        except OSError:
            continue
        if ip:
            with _lock:
                _cache[name] = (ip, now + TTL)
            return ip
        return None                       # the mesh says no such name
    return None


def address(host: str) -> str:
    """What to connect to: the mesh's answer for mesh names, else ``host`` unchanged (so the
    system resolver handles it)."""
    name = mesh_name(host)
    if name is None:
        return host
    return lookup(name) or name
