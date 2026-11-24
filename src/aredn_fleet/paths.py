"""Where the Fleet Manager keeps its files.

Normal install: per-user config (credentials list, pinned host keys, last
topology) and a firmware cache under the user's profile.

Portable mode: if a file named ``portable`` (or ``portable.txt``) sits next
to the executable, or ``AREDN_FLEET_HOME`` is set, everything lives in a
``data`` folder beside the exe instead, so the app can run from a USB stick
without touching the machine it's plugged into. Passwords saved through the
GUI still go to the OS credential store (Windows Credential Manager / Secret
Service), never into these folders.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller build
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def portable_root() -> Path | None:
    env = os.environ.get("AREDN_FLEET_HOME")
    if env:
        return Path(env)
    d = _exe_dir()
    if (d / "portable").exists() or (d / "portable.txt").exists():
        return d / "data"
    return None


def config_dir() -> Path:
    root = portable_root()
    if root:
        return root / "config"
    if os.name == "nt":
        new = Path(os.environ.get("APPDATA", Path.home())) / "aredn-fleet"
        _migrate(Path.home() / ".config" / "aredn-fleet", new)  # v0.1 dev builds used ~/.config
        return new
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "aredn-fleet"


def _migrate(old: Path, new: Path) -> None:
    if old.is_dir() and not new.exists():
        import shutil
        try:
            shutil.copytree(old, new)
        except OSError:
            pass


def cache_dir() -> Path:
    root = portable_root()
    if root:
        return root / "cache"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "aredn-fleet"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "aredn-fleet"


def firmware_dir() -> Path:
    return cache_dir() / "firmware"


def credentials_file() -> Path:
    return config_dir() / "credentials.json"


def known_hosts_file() -> Path:
    return config_dir() / "known_hosts"


def audit_file() -> Path:
    return config_dir() / "audit.jsonl"


def topology_file() -> Path:
    return config_dir() / "mesh.json"
