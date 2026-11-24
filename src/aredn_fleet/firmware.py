"""Firmware catalog and downloader, driven by the AREDN Firmware Selector data.

The AREDN Firmware Selector (AFS) publishes machine-readable metadata:

* ``afs/www/config.js``: a ``versions`` map of label -> ``data/<dir>`` for
  every published release (``4.26.10.0``) and nightly (``20261122-5e4131bf``).
* ``afs/www/data/<dir>/overview.json``: ``release``, ``image_url`` and a
  ``profiles`` list of ``{id, target, titles}``.
* ``afs/www/data/<dir>/<target>/<id>.json``: per-device details, including
  ``image_path``, ``supported_devices`` (OpenWrt board names) and ``images``
  (each with ``type``, ``name``, ``sha256`` and ``size``).

The default channel set is the two newest stable releases plus the newest
nightly. Every downloaded image is checked against its published SHA-256
before it is accepted. The base URL can point at a local mirror (AREDN
documents mirroring ``downloads.arednmesh.org::aredn_firmware`` with rsync),
which suits a manager host that sits on the mesh with no internet access.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

DEFAULT_BASE = "https://downloads.arednmesh.org/"
STABLE_RE = re.compile(r"^\d+(\.\d+){3}$")
NIGHTLY_RE = re.compile(r"^(\d{8})-[0-9a-f]+$")

Getter = Callable[[str], bytes]


def http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "aredn-fleet"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


# --------------------------------------------------------------------------- versions

def parse_versions(config_js: str) -> dict[str, str]:
    """Extract the ``versions`` map (label -> data dir) from AFS config.js."""
    m = re.search(r"versions\s*:\s*\{(.*?)\}", config_js, re.S)
    if not m:
        raise ValueError("no versions map in config.js")
    pairs = re.findall(r"""["']([^"']+)["']\s*:\s*["']([^"']+)["']""", m.group(1))
    return {label: path for label, path in pairs}


def _stable_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


@dataclass(frozen=True)
class Channel:
    name: str      # "stable-1", "stable-2", "nightly"
    version: str   # "4.26.10.0" / "20261122-5e4131bf"
    data_dir: str  # "data/4.26.10.0"


def select_channels(versions: dict[str, str], stable_count: int = 2,
                    nightly: bool = True) -> list[Channel]:
    stable = sorted((v for v in versions if STABLE_RE.match(v)), key=_stable_key, reverse=True)
    chans = [Channel(f"stable-{i + 1}", v, versions[v]) for i, v in enumerate(stable[:stable_count])]
    if nightly:
        nightlies = sorted((v for v in versions if NIGHTLY_RE.match(v)),
                           key=lambda v: NIGHTLY_RE.match(v).group(1), reverse=True)
        if nightlies:
            chans.append(Channel("nightly", nightlies[0], versions[nightlies[0]]))
    return chans


# --------------------------------------------------------------------------- catalog

@dataclass
class Image:
    type: str          # sysupgrade, factory, kernel, ...
    name: str
    sha256: str
    size: int
    url: str


@dataclass
class Profile:
    id: str
    target: str
    titles: list[str]
    supported_devices: list[str]
    images: list[Image] = field(default_factory=list)

    def image(self, kind: str = "sysupgrade") -> Image | None:
        return next((i for i in self.images if i.type == kind), None)


@dataclass
class Release:
    channel: str
    version: str
    profiles: dict[str, Profile]


def _title(t: dict) -> str:
    return " ".join(x for x in (t.get("vendor"), t.get("model"), t.get("variant")) if x)


class Catalog:
    def __init__(self, base_url: str = DEFAULT_BASE, get: Getter = http_get, workers: int = 8):
        self.base = base_url.rstrip("/") + "/"
        self.get = get
        self.workers = workers

    def _json(self, path: str) -> dict:
        return json.loads(self.get(self.base + path))

    def versions(self) -> dict[str, str]:
        return parse_versions(self.get(self.base + "afs/www/config.js").decode())

    def release(self, chan: Channel, profile_ids: Iterable[str] | None = None,
                boards: Iterable[str] | None = None) -> Release:
        overview = self._json(f"afs/www/{chan.data_dir}/overview.json")
        wanted = set(profile_ids) if profile_ids else None
        entries = [p for p in overview["profiles"] if wanted is None or p["id"] in wanted]

        def load(p: dict) -> Profile:
            d = self._json(f"afs/www/{chan.data_dir}/{p['target']}/{p['id']}.json")
            image_path = d.get("image_path") or ""
            imgs = [
                Image(i["type"], i["name"], i["sha256"], int(i.get("size", 0)),
                      f"{self.base}{image_path.strip('/')}/{i['name']}")
                for i in d.get("images", [])
            ]
            return Profile(p["id"], p["target"], [_title(t) for t in p.get("titles", [])],
                           list(d.get("supported_devices", [])), imgs)

        with ThreadPoolExecutor(self.workers) as pool:
            profiles = {p.id: p for p in pool.map(load, entries)}
        if boards:
            want = set(boards)
            profiles = {k: p for k, p in profiles.items() if want & set(p.supported_devices)}
        return Release(chan.name, overview.get("release", chan.version), profiles)


# --------------------------------------------------------------------------- last release per hardware

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class ReleaseIndex:
    """Which stable releases include a given piece of hardware, from AREDN's own catalog.

    AREDN drops hardware over time (the 32 MB ``ath79/tiny`` units stop at 3.25.5.1), so the
    newest release isn't always an option. Stable releases never change, so their overview and
    profile files are cached on disk and each is fetched once.
    """

    def __init__(self, catalog: Catalog, cache_dir: Path):
        self.cat = catalog
        self.dir = Path(cache_dir)

    def _cached(self, version: str, path: str) -> dict:
        f = self.dir / version / path.replace("/", "__")
        if f.exists():
            return json.loads(f.read_text())
        data = self.cat._json(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data))
        return data

    def stable_versions(self) -> list[tuple[str, str]]:
        v = self.cat.versions()
        return sorted(((k, d) for k, d in v.items() if STABLE_RE.match(k)),
                      key=lambda kv: _stable_key(kv[0]), reverse=True)

    def final_release(self, board: str = "", model: str = "") -> str | None:
        """Newest stable release with an image for this board (``/tmp/sysinfo/board_name``).
        Candidates come from the board name (OpenWrt ids are the board name with ``,`` → ``_``)
        or the model title; the board name in ``supported_devices`` decides."""
        want_id = board.replace(",", "_") if board else ""
        nm = _norm(model) if model else ""
        for ver, data_dir in self.stable_versions():
            ov = self._cached(ver, f"afs/www/{data_dir}/overview.json")
            for p in ov.get("profiles", []):
                titles = [_norm(_title(t)) for t in p.get("titles", [])]
                if not (p["id"] == want_id or (nm and nm in titles)):
                    continue
                if not board:
                    return ver
                d = self._cached(ver, f"afs/www/{data_dir}/{p['target']}/{p['id']}.json")
                if board in d.get("supported_devices", []):
                    return ver
        return None


# --------------------------------------------------------------------------- matching

def match_profile(release: Release, board_name: str = "", model: str = "") -> list[Profile]:
    """Find the profile(s) for a node.

    ``board_name`` (``/tmp/sysinfo/board_name`` on the node, e.g.
    ``glinet,gl-mt1300``) matched against ``supported_devices`` is
    authoritative. The free-text model from sysinfo.json is only a fallback
    and may return several candidates; the caller must refuse to proceed
    unless exactly one profile matches.
    """
    if board_name:
        hits = [p for p in release.profiles.values() if board_name in p.supported_devices]
        if hits:
            return hits
    if model:
        norm = re.sub(r"[^a-z0-9]", "", model.lower())
        return [p for p in release.profiles.values()
                if any(re.sub(r"[^a-z0-9]", "", t.lower()) in norm or
                       norm in re.sub(r"[^a-z0-9]", "", t.lower()) for t in p.titles)]
    return []


# --------------------------------------------------------------------------- download

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class FetchResult:
    path: Path
    status: str  # "downloaded", "cached", "failed"
    error: str = ""


class FirmwareStore:
    """Local cache laid out as ``<root>/<version>/<target>/<image name>``."""

    def __init__(self, root: Path, get: Getter = http_get):
        self.root = Path(root)
        self.get = get

    def path_for(self, release: Release, profile: Profile, image: Image) -> Path:
        return self.root / release.version / profile.target / image.name

    def fetch(self, release: Release, profile: Profile, image: Image) -> FetchResult:
        dest = self.path_for(release, profile, image)
        if dest.exists() and sha256_file(dest) == image.sha256:
            return FetchResult(dest, "cached")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            data = self.get(image.url)
            if hashlib.sha256(data).hexdigest() != image.sha256:
                raise ValueError("sha256 mismatch")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return FetchResult(dest, "downloaded")
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            tmp.unlink(missing_ok=True)
            return FetchResult(dest, "failed", str(exc))

    def fetch_release(self, release: Release, kinds: Iterable[str] = ("sysupgrade",),
                      workers: int = 4,
                      on_result: Callable[[FetchResult], None] | None = None) -> list[FetchResult]:
        jobs = [(p, i) for p in release.profiles.values() for i in p.images if i.type in set(kinds)]

        def one(pi):
            r = self.fetch(release, *pi)
            if on_result:
                on_result(r)
            return r

        with ThreadPoolExecutor(workers) as pool:
            results = list(pool.map(one, jobs))
        self.write_manifest(release)
        return results

    def write_manifest(self, release: Release) -> Path:
        out = self.root / release.version / "manifest.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "channel": release.channel, "version": release.version,
            "profiles": {k: asdict(v) for k, v in release.profiles.items()},
        }, indent=1))
        return out

    def load_release(self, version: str) -> Release:
        data = json.loads((self.root / version / "manifest.json").read_text())
        profiles = {
            k: Profile(v["id"], v["target"], v["titles"], v["supported_devices"],
                       [Image(**i) for i in v["images"]])
            for k, v in data["profiles"].items()
        }
        return Release(data["channel"], data["version"], profiles)

    def local_versions(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.parent.name for p in self.root.glob("*/manifest.json"))
