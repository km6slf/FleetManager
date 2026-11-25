"""Mesh names are looked up on the mesh node, inside Fleet Manager only."""
import socket
import struct
import threading

import pytest

from aredn_fleet import meshdns


@pytest.fixture
def fake_dns(monkeypatch):
    """A one-zone DNS server on localhost: knows node-a.local.mesh only."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    asked = []
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                q, peer = sock.recvfrom(512)
            except socket.timeout:
                continue
            qid = q[:2]
            pos, labels = 12, []
            while q[pos]:
                labels.append(q[pos + 1:pos + 1 + q[pos]].decode())
                pos += q[pos] + 1
            name = ".".join(labels)
            asked.append(name)
            question = q[12:pos + 5]
            if name == "node-a.local.mesh":
                ans = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("10.1.2.3")
                resp = qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + question + ans
            else:
                resp = qid + struct.pack(">HHHHH", 0x8183, 1, 0, 0, 0) + question
            sock.sendto(resp, peer)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    monkeypatch.setattr(meshdns, "PORT", sock.getsockname()[1])
    monkeypatch.delenv("AREDN_FLEET_DNS", raising=False)
    meshdns.set_server("127.0.0.1")
    yield asked
    stop.set()
    t.join()
    sock.close()
    meshdns.set_server(None)


def test_mesh_names_only():
    assert meshdns.mesh_name("node-a") == "node-a.local.mesh"
    assert meshdns.mesh_name("Node-A.local.mesh.") == "node-a.local.mesh"
    assert meshdns.mesh_name("example.com") is None
    assert meshdns.mesh_name("10.1.2.3") is None
    assert meshdns.mesh_name("localnode") is None and meshdns.mesh_name("localnode.local.mesh") is None


def test_lookup_asks_the_mesh_node_and_caches(fake_dns):
    assert meshdns.address("node-a") == "10.1.2.3"
    assert meshdns.address("node-a.local.mesh") == "10.1.2.3"
    assert fake_dns == ["node-a.local.mesh"]                 # second time from the cache


def test_unknown_or_other_names_fall_back_to_system(fake_dns):
    assert meshdns.address("nobody") == "nobody.local.mesh"  # system resolver gets the name
    assert meshdns.address("example.com") == "example.com"   # never asked of the mesh
    assert "example.com" not in fake_dns


def test_no_server_known_means_system_lookup(monkeypatch):
    monkeypatch.delenv("AREDN_FLEET_DNS", raising=False)
    meshdns.set_server(None)
    assert meshdns.address("node-a") == "node-a.local.mesh"


def test_server_learned_from_lan_address(monkeypatch):
    monkeypatch.delenv("AREDN_FLEET_DNS", raising=False)
    meshdns.set_server(None)
    meshdns.learn_from({"interfaces": [{"name": "wlan0", "ip": "10.98.1.1"},
                                       {"name": "br-lan", "ip": "10.136.250.61"}]})
    assert meshdns.server() == "10.136.250.61"
    meshdns.learn_from({}, "10.12.115.86")
    assert meshdns.server() == "10.12.115.86"
    meshdns.set_server(None)


def test_host_key_pinned_by_name_even_when_connecting_by_address(tmp_path):
    asyncssh = pytest.importorskip("asyncssh")
    from aredn_fleet.transport import _pinned_keys
    k1 = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode().strip()
    k2 = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode().strip()
    kh = tmp_path / "known_hosts"
    kh.write_text(f"[node-a.local.mesh]:2222 {k1}\n[node-b.local.mesh]:2222 {k2}\n")
    keys, ca, revoked = _pinned_keys("node-a.local.mesh", kh)
    assert [k.export_public_key("openssh").decode().strip() for k in keys] == [k1]
    assert _pinned_keys("node-c.local.mesh", kh)[0] == []
