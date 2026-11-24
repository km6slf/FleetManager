"""Credential sets for logging into nodes.

A fleet rarely shares one password. The manager keeps an ordered list of
credentials (SSH keys and passwords), tries them against each node over SSH
(AREDN listens on port 2222, user root), and remembers which one worked so
later jobs go straight to it.

Secrets are never written to the inventory file: passwords come from the
environment, a prompt, or the OS keyring; keys are referenced by path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

AREDN_SSH_PORT = 2222
AREDN_SSH_USER = "root"
KEYRING_SERVICE = "aredn-fleet"


def store_password(label: str, password: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, label, password)


def forget_password(label: str) -> None:
    import keyring
    try:
        keyring.delete_password(KEYRING_SERVICE, label)
    except keyring.errors.PasswordDeleteError:
        pass


@dataclass(frozen=True)
class Credential:
    label: str
    key_path: Optional[str] = None   # private key file (RSA, ECDSA or Ed25519)
    password_env: Optional[str] = None  # name of env var holding the password
    keyring: bool = False               # password kept in the OS credential store

    @property
    def kind(self) -> str:
        return "key" if self.key_path else "password"

    @property
    def source(self) -> str:
        if self.key_path:
            return self.key_path
        if self.keyring:
            return "OS credential store"
        return f"${self.password_env}"

    def password(self) -> str:
        if self.keyring:
            import keyring  # optional: pip install aredn-fleet[gui]
            return keyring.get_password(KEYRING_SERVICE, self.label) or ""
        return os.environ.get(self.password_env or "", "")


@dataclass
class CredentialStore:
    credentials: list[Credential] = field(default_factory=list)
    known_good: dict[str, str] = field(default_factory=dict)  # node -> credential label

    def order_for(self, node: str) -> list[Credential]:
        """Known-good credential first, then keys, then passwords."""
        good = self.known_good.get(node)
        ranked = sorted(self.credentials, key=lambda c: (c.label != good, c.kind != "key"))
        return ranked

    def record_success(self, node: str, cred: Credential) -> None:
        self.known_good[node] = cred.label

    def remove(self, label: str) -> None:
        self.credentials = [c for c in self.credentials if c.label != label]
        self.known_good = {n: l for n, l in self.known_good.items() if l != label}

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "credentials": [c.__dict__ for c in self.credentials],
            "known_good": self.known_good,
        }
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path) -> "CredentialStore":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls([Credential(**c) for c in data.get("credentials", [])],
                   dict(data.get("known_good", {})))
