import hashlib
import json

from aredn_fleet.firmware import (Catalog, FirmwareStore, match_profile, parse_versions,
                                  select_channels)

CONFIG_JS = """
var config = {
  show_help: true,
  // Path to where overview.json can be found
  versions: {
    "20261122-5e4131bf": "data/20261122-5e4131bf",
    "babel-20261122-5e4131bf": "data/20261122-5e4131bf",
    "20261101-aaaa1111": "data/20261101-aaaa1111",
    "4.26.10.0": "data/4.26.10.0",
    "4.26.7.0": "data/4.26.7.0",
    "4.26.1.0": "data/4.26.1.0",
    "3.25.10.0": "data/3.25.10.0",
  },
  default_version: "4.26.10.0",
  image_url: "/",
};
"""

BASE = "https://mirror.example/"


def fake_site(version="4.26.10.0", payload=b"FIRMWARE-BYTES"):
    sha = hashlib.sha256(payload).hexdigest()
    files = {
        BASE + "afs/www/config.js": CONFIG_JS.encode(),
        BASE + f"afs/www/data/{version}/overview.json": json.dumps({
            "release": version,
            "image_url": f"http://x/releases/{{target}}",
            "profiles": [
                {"id": "glinet_gl-mt1300", "target": "ramips/mt7621",
                 "titles": [{"vendor": "GL.iNet", "model": "GL-MT1300"}]},
                {"id": "ubnt_rocket-m", "target": "ath79/generic",
                 "titles": [{"vendor": "Ubiquiti", "model": "Rocket M", "variant": "XW"}]},
            ],
        }).encode(),
    }
    for pid, target, dev in [("glinet_gl-mt1300", "ramips/mt7621", "glinet,gl-mt1300"),
                             ("ubnt_rocket-m", "ath79/generic", "ubnt,rocket-m")]:
        name = f"aredn-{version}-{pid}-squashfs-sysupgrade.bin"
        files[BASE + f"afs/www/data/{version}/{target}/{pid}.json"] = json.dumps({
            "id": pid, "target": target, "supported_devices": [dev],
            "image_path": f"releases/4/26/{version}/targets/{target}",
            "images": [{"type": "sysupgrade", "name": name, "sha256": sha, "size": len(payload)},
                       {"type": "factory", "name": name.replace("sysupgrade", "factory"),
                        "sha256": sha, "size": len(payload)}],
        }).encode()
        files[BASE + f"releases/4/26/{version}/targets/{target}/{name}"] = payload
    calls = []

    def get(url):
        calls.append(url)
        return files[url]
    return get, calls


def test_parse_versions_and_channels():
    v = parse_versions(CONFIG_JS)
    chans = select_channels(v)
    assert [(c.name, c.version) for c in chans] == [
        ("stable-1", "4.26.10.0"), ("stable-2", "4.26.7.0"), ("nightly", "20261122-5e4131bf")]


def test_catalog_builds_image_urls():
    get, _ = fake_site()
    cat = Catalog(BASE, get=get)
    chan = select_channels(cat.versions())[0]
    rel = cat.release(chan)
    img = rel.profiles["glinet_gl-mt1300"].image()
    assert img.url == BASE + ("releases/4/26/4.26.10.0/targets/ramips/mt7621/"
                              "aredn-4.26.10.0-glinet_gl-mt1300-squashfs-sysupgrade.bin")
    assert rel.profiles["ubnt_rocket-m"].titles == ["Ubiquiti Rocket M XW"]


def test_fetch_verifies_and_caches(tmp_path):
    get, calls = fake_site()
    cat = Catalog(BASE, get=get)
    rel = cat.release(select_channels(cat.versions())[0])
    store = FirmwareStore(tmp_path, get=get)
    first = store.fetch_release(rel)
    assert [r.status for r in first] == ["downloaded", "downloaded"]  # sysupgrade only
    n = len(calls)
    second = store.fetch_release(rel)
    assert [r.status for r in second] == ["cached", "cached"] and len(calls) == n
    assert store.load_release("4.26.10.0").profiles.keys() == rel.profiles.keys()


def test_fetch_rejects_bad_hash(tmp_path):
    get, _ = fake_site()
    rel = Catalog(BASE, get=get).release(select_channels(parse_versions(CONFIG_JS))[0])
    store = FirmwareStore(tmp_path, get=lambda url: b"tampered")
    results = store.fetch_release(rel)
    assert all(r.status == "failed" and "sha256" in r.error for r in results)
    assert not any(tmp_path.rglob("*.bin"))


def test_match_profile_board_name_wins():
    get, _ = fake_site()
    rel = Catalog(BASE, get=get).release(select_channels(parse_versions(CONFIG_JS))[0])
    assert [p.id for p in match_profile(rel, board_name="ubnt,rocket-m")] == ["ubnt_rocket-m"]
    assert [p.id for p in match_profile(rel, model="GL.iNet GL-MT1300")] == ["glinet_gl-mt1300"]
    assert match_profile(rel, board_name="nope,nope") == []


def test_board_filter():
    get, _ = fake_site()
    rel = Catalog(BASE, get=get).release(select_channels(parse_versions(CONFIG_JS))[0],
                                         boards=["ubnt,rocket-m"])
    assert list(rel.profiles) == ["ubnt_rocket-m"]
