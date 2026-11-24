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
    async def download(self, remote: str) -> bytes: ...
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

    async def download(self, remote: str) -> bytes:
        r = await self._conn.run(f"cat {shlex.quote(remote)}", encoding=None, check=False, timeout=60)
        if r.exit_status != 0:
            raise OSError(f"couldn't read {remote} from {self.node}")
        return bytes(r.stdout or b"")

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()


from . import paths

DEFAULT_KNOWN_HOSTS = paths.known_hosts_file()


class HostKeyChanged(Exception):
    pass


async def pin_host_key(host: str, known_hosts: Path) -> None:
    """Trust on first use, then pin.

    AREDN keeps /etc/dropbear/*host_key in its sysupgrade keep list (seen on a
    4.26.1.0 node), so a node's host key should survive firmware upgrades. A
    key that changes anyway means a reset/reflash or an impostor, and the
    connection is refused until the operator removes the old entry.
    """
    import asyncssh

    entry = f"[{host}]:{AREDN_SSH_PORT}"
    if known_hosts.exists() and any(line.split(" ", 1)[0] == entry
                                    for line in known_hosts.read_text().splitlines()):
        return
    import asyncio
    key = await asyncio.wait_for(asyncssh.get_server_host_key(host, AREDN_SSH_PORT), timeout=20)
    if key is None:
        raise ConnectionError(f"{host}: no SSH host key offered")
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    with known_hosts.open("a") as f:
        f.write(f"{entry} {key.export_public_key('openssh').decode().strip()}\n")


def _connect_kwargs(cred: Credential, known_hosts: Path) -> dict:
    kw: dict = {"username": AREDN_SSH_USER, "port": AREDN_SSH_PORT,
                "known_hosts": str(known_hosts), "connect_timeout": 15}
    if cred.key_path:
        kw["client_keys"] = [os.path.expanduser(cred.key_path)]
        kw["password"] = None
    else:
        pw = cred.password()
        if not pw:
            raise AuthFailed(f"no password available for credential {cred.label!r}")
        kw["password"] = pw
        kw["client_keys"] = None
    return kw


async def connect(node: str, store: CredentialStore, host_suffix: str = ".local.mesh",
                  known_hosts: Path = DEFAULT_KNOWN_HOSTS) -> SSHSession:
    """Try each credential in turn and remember the first that works."""
    import asyncssh  # optional dependency: pip install aredn-fleet[ssh]

    host = node + host_suffix
    await pin_host_key(host, known_hosts)
    errors = []
    for cred in store.order_for(node):
        try:
            conn = await asyncssh.connect(host, **_connect_kwargs(cred, known_hosts))
        except asyncssh.HostKeyNotVerifiable as exc:
            raise HostKeyChanged(
                f"{node}: host key does not match {known_hosts}; refusing to connect. "
                "If the node was reset or replaced, remove its line and retry.") from exc
        except (asyncssh.PermissionDenied, AuthFailed) as exc:
            errors.append(f"{cred.label}: {exc}")
            continue
        store.record_success(node, cred)
        return SSHSession(node, conn)
    raise AuthFailed(f"{node}: no credential worked ({'; '.join(errors) or 'none configured'})")
