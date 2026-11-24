import json

from aredn_fleet.discovery import crawl, link_entries, parse_node
from aredn_fleet.firmware import Catalog, ReleaseIndex

# Shape of /cgi-bin/sysinfo.json on AREDN 3.23.8.0 (api_version 1.11, Lua UI), from a real
# NanoStation M2 XM; MACs replaced.
OLD = {
    "meshrf": {"ssid": "AREDN-10-v3", "channel": "-2", "status": "on", "freq": "2397", "chanbw": "10"},
    "link_info": [],
    "tunnels": {"active_tunnel_count": "0"},
    "sysinfo": {"uptime": "0 days, 0:02:55", "loads": [0.31, 0.35, 0.16]},
    "interfaces": [{"mac": "02:00:00:00:00:01", "name": "br-dtdlink", "ip": "10.99.62.143"}],
    "api_version": "1.11",
    "node": "KA6TCR-NSM2-CAM-143",
    "node_details": {"model": "Ubiquiti Nanostation M (XM)", "mesh_gateway": "0", "firmware_mfg": "AREDN",
                     "board_id": "0xe012", "firmware_version": "3.23.8.0"},
}


def test_old_sysinfo_parses():
    info, neigh = parse_node(OLD)
    assert info.name == "ka6tcr-nsm2-cam-143" and info.firmware == "3.23.8.0"
    assert info.api_version == "1.11" and info.board_id == "0xe012" and neigh == []


def test_link_info_as_list_or_map():
    assert link_entries({"link_info": []}) == []
    assert link_entries({"link_info": [{"hostname": "a"}]}) == [{"hostname": "a"}]
    assert link_entries({"link_info": {"10.0.0.1": {"hostname": "a", "linkType": ""}}}) == \
        [{"hostname": "a", "linkType": ""}]
    old_with_link = {**OLD, "link_info": {"10.1.2.3": {"hostname": "other.local.mesh", "linkType": ""}}}
    _, neigh = parse_node(old_with_link)
    assert neigh == [("other", "RF")]       # empty linkType on old firmware counts as RF


def test_crawl_mixed_firmware():
    new = {"node": "hap", "api_version": "2.0", "node_details": {"firmware_version": "3.25.5.1"},
           "link_info": {"10.1.1.1": {"hostname": "ka6tcr-nsm2-cam-143.local.mesh", "linkType": "DTD"}}}
    data = {"localnode": new, "hap": new, "ka6tcr-nsm2-cam-143": OLD}
    res = crawl("localnode", fetch=lambda n: data[n])
    assert res.nodes["ka6tcr-nsm2-cam-143"].firmware == "3.23.8.0"
    assert {(l.a, l.b, l.kind) for l in res.topology.links} >= {("hap", "ka6tcr-nsm2-cam-143", "DTD")} or \
        {(l.b, l.a, l.kind) for l in res.topology.links} >= {("hap", "ka6tcr-nsm2-cam-143", "DTD")}


def fake_catalog(tmp_path):
    """Three releases: the NanoStation M XM is in 3.25.5.1 but not 4.26.10.0."""
    files = {
        "afs/www/config.js": 'var config = { versions: {"4.26.10.0": "data/4.26.10.0", '
                             '"3.25.5.1": "data/3.25.5.1", "3.23.8.0": "data/3.23.8.0", '
                             '"20261122-5e4131bf": "data/20261122-5e4131bf"} };',
        "afs/www/data/4.26.10.0/overview.json": {"profiles": [
            {"id": "ubnt_nanostation-m-xw", "target": "ath79/generic",
             "titles": [{"vendor": "Ubiquiti", "model": "Nanostation M", "variant": "XW"}]}]},
        "afs/www/data/4.26.10.0/ath79/generic/ubnt_nanostation-m-xw.json":
            {"supported_devices": ["ubnt,nanostation-m-xw"]},
        "afs/www/data/3.25.5.1/overview.json": {"profiles": [
            {"id": "ubnt_nanostation-m", "target": "ath79/tiny",
             "titles": [{"vendor": "Ubiquiti", "model": "Nanostation M", "variant": "XM"}]}]},
        "afs/www/data/3.25.5.1/ath79/tiny/ubnt_nanostation-m.json": {"supported_devices": ["ubnt,nanostation-m"]},
        "afs/www/data/3.23.8.0/overview.json": {"profiles": []},
    }
    calls = []

    def get(url):
        path = url.split("/", 3)[3]
        calls.append(path)
        v = files[path]
        return (v if isinstance(v, str) else json.dumps(v)).encode()
    return ReleaseIndex(Catalog("http://x/", get=get), tmp_path / "cat"), calls


def test_final_release_for_hardware(tmp_path):
    idx, calls = fake_catalog(tmp_path)
    assert idx.final_release("ubnt,nanostation-m", "Ubiquiti Nanostation M (XM)") == "3.25.5.1"
    assert idx.final_release("ubnt,nanostation-m-xw") == "4.26.10.0"
    assert idx.final_release("ubnt,something-else") is None
    n = len(calls)
    idx.final_release("ubnt,nanostation-m", "")
    assert len([c for c in calls[n:] if "overview" in c or c.endswith(".json") and "config" not in c]) == 0
