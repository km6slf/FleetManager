"""SSH access to AREDN nodes (dropbear on port 2222, user root).

``NodeSession`` is the narrow interface that jobs use: run a command and
upload a file. Uploads stream into ``cat > /tmp/...`` over an exec channel
rather than using SFTP, because OpenWrt images usually ship without an SFTP
server. Tests substitute a fake that implements the same two methods.
"""

from __future__ import annotations

import asyncio
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


async def pin_host_key(host: str, known_hosts: Path, addr: str | None = None) -> None:
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
    key = await asyncio.wait_for(asyncssh.get_server_host_key(addr or host, AREDN_SSH_PORT),
                                 timeout=20)
    if key is None:
        raise ConnectionError(f"{host}: no SSH host key offered")
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    with known_hosts.open("a") as f:
        f.write(f"{entry} {key.export_public_key('openssh').decode().strip()}\n")


def _pinned_keys(host: str, known_hosts: Path):
    """The host keys pinned for ``host`` (by name), for asyncssh's ``known_hosts`` check."""
    import asyncssh
    entry = f"[{host}]:{AREDN_SSH_PORT}"
    keys = []
    for line in known_hosts.read_text().splitlines() if known_hosts.exists() else []:
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0] == entry:
            keys.append(asyncssh.import_public_key(parts[1].strip()))
    return (keys, [], [])


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

    from . import meshdns
    host = node + host_suffix
    # Connect to the address the mesh node gives (not Windows' DNS), but pin and check the host
    # key under the node's name, so an address change doesn't look like a different node.
    addr = await asyncio.to_thread(meshdns.address, host)
    # Reaching the node is retried (lossy links); wrong credentials and changed keys are not.
    await with_retries(lambda: pin_host_key(host, known_hosts, addr))
    pinned = _pinned_keys(host, known_hosts)
    errors = []
    for cred in store.order_for(node):
        try:
            conn = await with_retries(lambda: asyncssh.connect(
                addr, **{**_connect_kwargs(cred, known_hosts), "known_hosts": pinned}))
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


def detached(cmd: str, log: str = "/dev/null") -> str:
    """Shell line that starts ``cmd`` in the background so it survives the SSH session ending.

    AREDN's BusyBox has no ``nohup``; ignoring SIGHUP in a subshell and exec'ing the command
    does the same job (an ignored signal stays ignored across exec).
    """
    return f"( trap '' HUP; exec {cmd} ) >{log} 2>&1 </dev/null &"


def errtext(exc: BaseException) -> str:
    """An exception as text a person can act on. Timeouts have no message of their own."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timed out (no answer from the node)"
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def transient(exc: BaseException) -> bool:
    """A failure worth trying again: the node or link was slow or dropped packets. Wrong
    credentials and changed host keys are not retried."""
    if isinstance(exc, (AuthFailed, HostKeyChanged)):
        return False
    try:
        import asyncssh
        if isinstance(exc, (asyncssh.PermissionDenied, asyncssh.HostKeyNotVerifiable)):
            return False
        if isinstance(exc, (asyncssh.ConnectionLost, asyncssh.DisconnectError)):
            return True
    except ImportError:
        pass
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError))


async def with_retries(fn, attempts: int = 3, delays: tuple[float, ...] = (3.0, 8.0),
                       on_retry=None, sleep=None):
    """Run a **read-only** step again when it fails for a transient reason (mesh links drop
    packets in bursts). Never use this for steps that change a node."""
    sleep = sleep or asyncio.sleep
    for i in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            if i == attempts - 1 or not transient(exc):
                raise
            if on_retry:
                on_retry(i + 1, exc)
            await sleep(delays[min(i, len(delays) - 1)])
