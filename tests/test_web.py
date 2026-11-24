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
    r = client.post("/api/radio", json={"nodes": ["far1"],
                                        "changes": [{"band": "5GHz", "mode": "meshptp"}]})
    assert r.status_code == 400 and "peer" in r.json()["detail"]
    r = client.post("/api/radio", json={"nodes": ["far1"], "changes": [{"band": "5GHz", "channel": 133}],
                                        "execute": True})
    assert r.status_code == 400 and "switch" in r.json()["detail"]


def _radio(band, mode, channel, bw, table, bws, oob=()):
    return {"iface": "wlan0", "mode": {"mode": mode, "channel": channel, "bandwidth": bw},
            "def": {"band": band}, "bws": bws, "channels": table, "managedOOB": list(oob)}


def _c(n, f):
    return {"number": n, "label": f"{n} ({f})", "frequency": f}


def test_radio_options_intersect_per_band(client):
    from aredn_fleet.jobs.radio import parse_radio_inventory
    st = client.state
    five_a = {"10": [_c(131, 5655), _c(133, 5665), _c(177, 5885)], "20": [_c(131, 5655)]}
    five_b = {"10": [_c(133, 5665), _c(177, 5885)], "5": [_c(133, 5665)]}
    halow = {"1": [_c(1, 903), _c(3, 905)], "2": [_c(2, 904)]}
    three = {"10": [_c(76, 3380), _c(84, 3420)]}
    st.set_radios("far1", parse_radio_inventory(json.dumps({"config": [
        _radio("5GHz", "mesh", 131, 10, five_a, [10, 20], oob=[177]),
        _radio("900MHz", "mesh", 1, 1, halow, [1, 2])]}), "single-mesh"))
    st.set_radios("far2", parse_radio_inventory(json.dumps({"config": [
        _radio("5GHz", "mesh", 133, 10, five_b, [5, 10])]}), "single-mesh"))
    st.set_radios("relay", parse_radio_inventory(json.dumps({"config": [
        _radio("3GHz", "mesh", 76, 10, three, [10])]}), "single-mesh"))

    o = client.post("/api/radio/options", json={"nodes": ["far1", "far2", "relay", "hub"]}).json()
    assert o["missing"] == ["hub"]
    bands = {b["band"]: b for b in o["bands"]}
    assert [b["band_label"] for b in o["bands"]] == ["900 MHz", "3.4 GHz", "5 GHz"]  # low to high
    five = bands["5GHz"]
    assert five["nodes"] == ["far1", "far2"] and five["bws"] == [10]
    assert [c["number"] for c in five["channels"]["mesh"]["10"]] == [133, 177]
    assert [c["number"] for c in five["channels"]["managed"]["10"]] == [133]   # far1 hides 177
    assert five["per_node"]["far1"]["label"] == "131 (5655)"
    assert bands["900MHz"]["nodes"] == ["far1"] and bands["900MHz"]["bws"] == [1, 2]

    topo = client.get("/api/topology").json()
    far1 = next(n for n in topo["nodes"] if n["name"] == "far1")
    assert [(r["band_label"], r["mode_label"], r["label"]) for r in far1["radios"]] == [
        ("900 MHz", "Mesh", "1 (903)"), ("5 GHz", "Mesh", "131 (5655)")]   # low to high


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


def _hap(ch=131, bw=10, ssid="AREDN"):
    five = {"10": [_c(131, 5655), _c(133, 5665), _c(149, 5745)], "20": [_c(131, 5655), _c(149, 5745)]}
    return {"config": [{"iface": "wlan0", "mode": {"mode": "mesh", "channel": ch, "bandwidth": bw, "ssid": ssid},
                        "def": {"band": "5GHz"}, "bws": [10, 20], "channels": five}]}


def test_node_specific_settings_roundtrip_and_marker(client):
    from aredn_fleet.jobs.radio import parse_radio_inventory
    st = client.state
    # can't save before the node's radios are known
    r = client.put("/api/overrides/far1", json={"radio": {"5GHz": {"channel": 149}}})
    assert r.status_code == 400 and "read far1's radios" in r.json()["detail"]
    st.set_radios("far1", parse_radio_inventory(json.dumps(_hap()), "single-mesh"))
    # checked against the node's own channel list
    r = client.put("/api/overrides/far1", json={"radio": {"5GHz": {"channel": 132}}})
    assert r.status_code == 400 and "isn't offered" in r.json()["detail"]
    r = client.put("/api/overrides/far1", json={"radio": {"5GHz": {"ssid": "bad ssid"}}})
    assert r.status_code == 400 and "SSID" in r.json()["detail"]
    r = client.put("/api/overrides/far1", json={"radio": {"5GHz": {"channel": 149, "ssid": "IDEC"}},
                                                 "note": "PtP backhaul"})
    assert r.status_code == 200
    far1 = next(n for n in client.get("/api/topology").json()["nodes"] if n["name"] == "far1")
    assert far1["custom"]["text"] == "5 GHz: channel 149, SSID IDEC" and far1["custom"]["note"] == "PtP backhaul"
    # survives a restart (stored separately from discovery)
    assert AppState(fw_dir=st.fw_dir, config_dir=st.config_dir).overrides["far1"]["radio"]["5GHz"]["channel"] == 149
    # empty settings = back to following the group
    client.put("/api/overrides/far1", json={"radio": {"5GHz": {}}})
    assert "far1" not in st.overrides


def test_radio_job_allows_only_node_settings_but_not_nothing(client):
    from aredn_fleet.jobs.radio import parse_radio_inventory
    st = client.state
    r = client.post("/api/radio", json={"nodes": ["far1", "far2"], "changes": []})
    assert r.status_code == 400 and "nothing to change" in r.json()["detail"]
    st.set_radios("far1", parse_radio_inventory(json.dumps(_hap()), "single-mesh"))
    client.put("/api/overrides/far1", json={"radio": {"5GHz": {"channel": 149}}})
    r = client.post("/api/radio", json={"nodes": ["far1", "far2"], "changes": []})
    assert r.status_code == 200          # far1's own settings make it a real job (dry run)


def test_group_ssid_is_validated(client):
    r = client.post("/api/radio", json={"nodes": ["far1"], "changes": [{"band": "5GHz", "ssid": "no spaces allowed"}]})
    assert r.status_code == 400 and "SSID" in r.json()["detail"]


def test_execute_requires_a_matching_recent_review():
    from aredn_fleet.web.state import Job, JobManager
    jm = JobManager()
    ok = Job("r1", "radio", "dry", False, status="done", fingerprint="abc", dry_run=True,
             finished=time.time(), result={"blocked": []})
    jm.jobs["r1"] = ok
    assert jm.reviewed("r1", "radio", "abc") is None
    assert "review the plan first" in jm.reviewed(None, "radio", "abc")
    assert "review the plan first" in jm.reviewed("r1", "upgrade", "abc")          # wrong kind
    assert "something changed" in jm.reviewed("r1", "radio", "xyz")               # plan changed
    ok.finished = time.time() - 3600
    assert "15 minutes" in jm.reviewed("r1", "radio", "abc")                        # stale
    ok.finished, ok.result = time.time(), {"blocked": ["far1"]}
    assert "blocked" in jm.reviewed("r1", "radio", "abc")
    jm.jobs["e1"] = Job("e1", "radio", "exec", True, status="done", fingerprint="abc", dry_run=False,
                        finished=time.time(), result={"blocked": []})
    assert "review the plan first" in jm.reviewed("e1", "radio", "abc")             # not a dry run


def test_upgrade_execute_without_review_is_refused(client):
    from aredn_fleet.firmware import FirmwareStore, Image, Profile, Release
    img = Image("sysupgrade", "x.bin", "0" * 64, 1, "u")
    FirmwareStore(client.state.fw_dir).write_manifest(
        Release("stable-1", "4.26.10.0", {"p": Profile("p", "t/g", ["X"], ["x,y"], [img])}))
    r = client.post("/api/upgrade", json={"nodes": ["far1"], "version": "4.26.10.0",
                                          "execute": True, "confirm": "upgrade"})
    assert r.status_code == 409 and "review the plan first" in r.json()["detail"]


def _wait(client, job_id):
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job still running")


def test_radio_review_ledger_and_changed_plan_is_refused(client):
    from aredn_fleet.transport import CommandResult
    st = client.state

    async def fake_connect(node):
        class S:
            async def run(self, cmd, timeout=60):
                if cmd.startswith("ucode"):
                    return CommandResult(0, json.dumps(_hap()), "")
                if "radio-and-antenna" in cmd:
                    return CommandResult(0, "single-mesh\n", "")
                if cmd.startswith("[ -f /etc/rc.local"):
                    return CommandResult(0, "ok\n", "")
                if cmd.startswith("date"):
                    return CommandResult(0, str(int(time.time())), "")
                return CommandResult(0, "", "")

            async def close(self):
                pass
        return S()

    async def no_links(node):
        return {"link_info": {}}

    st.connector, st.sysinfo = fake_connect, no_links
    body = {"nodes": ["far1", "far2"], "changes": [{"band": "5GHz", "channel": 149, "ssid": "IDEC"}]}
    job = client.post("/api/radio", json=body).json()
    j = _wait(client, job["id"])
    assert j["status"] == "done" and j["dry_run"]
    rows = {n["node"]: n for c in j["result"]["clusters"] for n in c["nodes"]}
    led = rows["far1"]["ledger"][0]
    assert led["band_label"] == "5 GHz" and led["changed"]
    assert led["before"]["channel"] == "131 (5655)" and led["after"]["channel"] == "149 (5745)"
    assert led["before"]["ssid"] == "AREDN-10-v3" and led["after"]["ssid"] == "IDEC-10-v3"
    assert led["before"]["width"] == led["after"]["width"] == "10 MHz"
    # the reviewed plan is what may run: changing a node's own settings afterwards invalidates it
    st.set_radios("far1", __import__("aredn_fleet.jobs.radio", fromlist=["x"]).parse_radio_inventory(
        json.dumps(_hap()), "single-mesh"))
    client.put("/api/overrides/far1", json={"radio": {"5GHz": {"channel": 133}}})
    r = client.post("/api/radio", json={**body, "execute": True, "confirm": "switch", "reviewed_job": job["id"]})
    assert r.status_code == 409 and "something changed" in r.json()["detail"]
