import json
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aredn_fleet.discovery import NodeInfo  # noqa: E402
from aredn_fleet.firmware import FirmwareStore, Image, Profile, Release  # noqa: E402
from aredn_fleet.topology import Topology  # noqa: E402
from aredn_fleet.web.app import create_app  # noqa: E402
from aredn_fleet.web.state import AppState  # noqa: E402

TOKEN = "test-token"


@pytest.fixture
def client(tmp_path):
    st = AppState(fw_dir=tmp_path / "fw", config_dir=tmp_path / "cfg")
    t = Topology()
    for a, b, k in [("hub", "relay", "RF"), ("relay", "far1", "RF"), ("relay", "far2", "RF"),
                    ("hub", "eoc", "DTD")]:
        t.add_link(a, b, k)
    infos = {n: NodeInfo(n, firmware="4.26.1.0", model="MikroTik hAP ac lite",
                         raw={"meshrf": {"channel": 131, "chanbw": 20}}) for n in t.nodes}
    st.set_discovery(t, infos, {}, "hub")
    async def no_ssh(node):
        raise ConnectionError(f"{node}: unreachable in tests")

    async def no_sysinfo(node):
        return None

    st.connector, st.sysinfo = no_ssh, no_sysinfo
    c = TestClient(create_app(st, token=TOKEN))
    c.get(f"/?token={TOKEN}")  # sets the auth cookie
    c.state = st
    return c


def test_api_requires_token(tmp_path):
    c = TestClient(create_app(AppState(fw_dir=tmp_path / "f", config_dir=tmp_path / "c"), token=TOKEN))
    assert c.get("/api/info").status_code == 401
    assert c.get("/api/info", headers={"x-fleet-token": "nope"}).status_code == 401
    assert c.get("/api/info", headers={"x-fleet-token": TOKEN}).status_code == 200
    assert c.get("/?token=wrong").status_code == 401
    assert c.get("/").status_code == 200  # the page itself is harmless


def test_topology_view(client):
    v = client.get("/api/topology").json()
    by = {n["name"]: n for n in v["nodes"]}
    assert v["root"] == "hub"
    assert by["relay"]["hop"] == 1 and by["relay"]["cuts_off"] == ["far1", "far2"]
    assert by["far1"]["channel"] == 131
    assert {l["kind"] for l in v["links"]} == {"RF", "DTD"}


def test_plan_orders_edge_first(client):
    p = client.post("/api/plan", json={"nodes": ["relay", "far1", "far2"], "parallel": 2}).json()
    assert p["waves"] == [["far1", "far2"], ["relay"]]


def test_upgrade_requires_typed_confirmation(client):
    r = client.post("/api/upgrade", json={"nodes": ["far1"], "version": "x", "execute": True})
    assert r.status_code == 400 and "confirm" in r.json()["detail"]


def test_upgrade_needs_downloaded_version(client):
    r = client.post("/api/upgrade", json={"nodes": ["far1"], "version": "4.26.10.0"})
    assert r.status_code == 400 and "isn't downloaded" in r.json()["detail"]


def test_radio_validation(client):
    r = client.post("/api/radio", json={"nodes": ["far1"], "mode": "meshptp"})
    assert r.status_code == 400 and "peer" in r.json()["detail"]
    r = client.post("/api/radio", json={"nodes": ["far1"], "channel": 133, "execute": True})
    assert r.status_code == 400 and "switch" in r.json()["detail"]


def test_credentials_key_roundtrip(client, tmp_path):
    key = tmp_path / "id_test"
    key.write_text("dummy")
    assert client.post("/api/creds", json={"label": "k", "kind": "key", "key_path": str(key)}).json()["ok"]
    assert client.post("/api/creds", json={"label": "bad", "kind": "key",
                                           "key_path": str(tmp_path / "missing")}).status_code == 400
    listed = client.get("/api/creds").json()["credentials"]
    assert listed == [{"label": "k", "kind": "key", "source": str(key)}]
    assert client.delete("/api/creds/k").json()["ok"]
    assert client.get("/api/creds").json()["credentials"] == []


def test_firmware_local_and_job_flow(client, tmp_path):
    img = Image("sysupgrade", "x.bin", "0" * 64, 1, "u")
    prof = Profile("p", "t/g", ["X"], ["x,y"], [img])
    FirmwareStore(client.state.fw_dir).write_manifest(Release("stable-1", "4.26.10.0", {"p": prof}))
    rel = client.get("/api/firmware/local").json()["releases"]
    assert rel == [{"version": "4.26.10.0", "channel": "stable-1", "profiles": 1, "images": 0}]

    # a dry run job runs in the background and reports through the job API
    job = client.post("/api/upgrade", json={"nodes": ["far1"], "version": "4.26.10.0"}).json()
    for _ in range(100):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert j["status"] == "done"
    msgs = [e["msg"] for e in j["events"]]
    assert "Dry run" in msgs[0] and any("far1: failed connect" in m for m in msgs)
    assert client.get("/api/jobs").json()["jobs"][0]["id"] == job["id"]


def test_static_assets_served(client):
    assert "Fleet Manager" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_starts_without_console_streams(tmp_path):
    """A windowed Windows exe has sys.stdout/stderr = None; startup must survive that."""
    import subprocess
    import sys
    code = (
        "import sys; sys.stdout = None; sys.stderr = None\n"
        "from aredn_fleet.desktop import ensure_std_streams, start_server, _free_port\n"
        "ensure_std_streams()\n"
        "s, t = start_server('127.0.0.1', _free_port(), 'tok')\n"
        "s.should_exit = True; t.join(5)\n"
        "open(sys.argv[1], 'w').write('ok')\n"
    )
    marker = tmp_path / "ok"
    env = {**__import__("os").environ, "AREDN_FLEET_HOME": str(tmp_path / "home")}
    subprocess.run([sys.executable, "-c", code, str(marker)], env=env, timeout=60, check=True)
    assert marker.read_text() == "ok"
    assert (tmp_path / "home/config/logs/fleet-manager.log").exists()
