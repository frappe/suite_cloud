import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


@dataclass
class SSHTarget:
    host: str
    user: str
    port: int
    private_key: str = field(repr=False)
    host_keys: str | None = None  # known_hosts lines recorded at Verify SSH; None means never verified


class UnknownHostError(Exception):
    """No host key is recorded for the server, so nothing could tell it from an impostor."""


KEYSCAN_TIMEOUT = 15


def scan_host_keys(host: str, port: int) -> str:
    """The server's host keys as known_hosts lines, from ssh-keyscan.

    Recorded once, on the first successful Verify SSH (trust on first use, the moment the
    operator has just put the cluster's key on the box), and required to match afterwards.
    """

    result = subprocess.run(
        ["ssh-keyscan", "-T", str(KEYSCAN_TIMEOUT), "-p", str(int(port)), host],
        capture_output=True,
        text=True,
        timeout=KEYSCAN_TIMEOUT + 5,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise UnknownHostError(f"No SSH host key could be read from {host}:{port}")
    return "\n".join(sorted(lines))


@contextmanager
def known_hosts_file(target: SSHTarget) -> Iterator[str]:
    """The pinned host keys as a known_hosts file for the duration of a play."""

    if not target.host_keys:
        raise UnknownHostError(f"{target.host}: verify SSH first so its host key is recorded")
    fd, path = tempfile.mkstemp(prefix="suite-cloud-", suffix=".known_hosts", dir=scratch_dir())
    try:
        with os.fdopen(fd, "w") as f:
            f.write(target.host_keys.rstrip("\n") + "\n")
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


def generate_keypair(comment: str) -> tuple[str, str]:
    """Returns (OpenSSH private key PEM, authorized_keys line) for a fresh ed25519 key."""

    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return private, f"{public} {comment}"


@contextmanager
def private_key_file(private_key: str) -> Iterator[str]:
    """Writes the key to a 0600 temp file for the duration of a play, then removes it."""

    fd, path = tempfile.mkstemp(prefix="suite-cloud-", suffix=".key", dir=scratch_dir())
    try:
        with os.fdopen(fd, "w") as f:
            f.write(private_key.rstrip("\n") + "\n")
        os.chmod(path, 0o600)
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


def scratch_dir() -> str:
    """A 0700 directory of the site for keys and runner data, not the world-readable system temp."""

    import frappe

    path = frappe.get_site_path("private", "provisioning")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# A login name and nothing else: the inventory is INI, where a space starts a new variable and a
# newline a new host, so a crafted user could inject ansible_connection or a ProxyCommand.
SSH_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def validate_ssh_user(value: str | None) -> str:
    user = (value or "").strip()
    if not SSH_USER.match(user):
        raise ValueError(f"SSH user {value!r} must be a plain login name: lowercase letters, digits, _ and -")
    return user


def validate_ssh_user_field(doc) -> None:
    """The document-side check, so a bad value is refused at save with a readable message."""

    if doc.get("ssh_user"):
        try:
            doc.ssh_user = validate_ssh_user(doc.ssh_user)
        except ValueError:
            import frappe
            from frappe import _

            frappe.throw(_("SSH User must be a plain login name: lowercase letters, digits, _ and -."))


def inventory_line(alias: str, target: SSHTarget, key_path: str, known_hosts_path: str) -> str:
    """One INI inventory host; the connection is refused unless the host key matches the pinned one."""

    user = validate_ssh_user(target.user)
    return (
        f"{alias} ansible_host={target.host} ansible_user={user} ansible_port={int(target.port)} "
        f"ansible_ssh_private_key_file={key_path} "
        f"ansible_ssh_common_args='-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known_hosts_path}'"
    )
