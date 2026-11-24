"""SSH access to AREDN nodes (dropbear on port 2222, user root).

``NodeSession`` is the narrow interface that jobs use: run a command and
upload a file. Uploads stream into ``cat > /tmp/...`` over an exec channel
rather than using SFTP, because OpenWrt images usually ship without an SFTP
server. Tests substitute a fake that implements the same two methods.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .credentials import AREDN_SSH_PORT, AREDN_SSH_USER, Credential, CredentialStore


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0


class NodeSession(Protocol):
    node: str

    async def run(self, command: str, timeout: float = 60) -> CommandResult: ...
    async def upload(self, local: Path, remote: str) -> None: ...
    async def close(self) -> None: ...


class AuthFailed(Exception):
    pass


class SSHSession:
    def __init__(self, node: str, conn):
        self.node = node
        self._conn = conn

    async def run(self, command: str, timeout: float = 60) -> CommandResult:
        r = await self._conn.run(command, check=False, timeout=timeout)
        return CommandResult(r.exit_status if r.exit_status is not None else -1,
                             str(r.stdout or ""), str(r.stderr or ""))

    async def upload(self, local: Path, remote: str) -> None:
        cmd = f"cat > {shlex.quote(remote)}"
        async with self._conn.create_process(cmd, encoding=None) as proc:
            with open(local, "rb") as f:
                for chunk in iter(lambda: f.read(64 * 1024), b""):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            proc.stdin.write_eof()
            await proc.wait(check=True)

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()


def _connect_kwargs(cred: Credential) -> dict:
    kw: dict = {"username": AREDN_SSH_USER, "port": AREDN_SSH_PORT,
                # TODO(safety): host-key checking is off for now. Before field
                # use, pin keys in a fleet known_hosts file and decide how to
                # handle a key that changes after a reflash.
                "known_hosts": None, "connect_timeout": 15}
    if cred.key_path:
        kw["client_keys"] = [os.path.expanduser(cred.key_path)]
        kw["password"] = None
    else:
        pw = os.environ.get(cred.password_env or "", "")
        if not pw:
            raise AuthFailed(f"password env var {cred.password_env!r} is empty")
        kw["password"] = pw
        kw["client_keys"] = None
    return kw


async def connect(node: str, store: CredentialStore, host_suffix: str = ".local.mesh") -> SSHSession:
    """Try each credential in turn and remember the first that works."""
    import asyncssh  # optional dependency: pip install aredn-fleet[ssh]

    errors = []
    for cred in store.order_for(node):
        try:
            conn = await asyncssh.connect(node + host_suffix, **_connect_kwargs(cred))
        except (asyncssh.PermissionDenied, AuthFailed) as exc:
            errors.append(f"{cred.label}: {exc}")
            continue
        store.record_success(node, cred)
        return SSHSession(node, conn)
    raise AuthFailed(f"{node}: no credential worked ({'; '.join(errors) or 'none configured'})")
