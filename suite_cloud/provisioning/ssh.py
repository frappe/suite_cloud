import os
import re
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

    fd, path = tempfile.mkstemp(prefix="suite-cloud-", suffix=".key")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(private_key.rstrip("\n") + "\n")
        os.chmod(path, 0o600)
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


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


def inventory_line(alias: str, target: SSHTarget, key_path: str) -> str:
    user = validate_ssh_user(target.user)
    return (
        f"{alias} ansible_host={target.host} ansible_user={user} ansible_port={int(target.port)} "
        f"ansible_ssh_private_key_file={key_path} "
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'"
    )
