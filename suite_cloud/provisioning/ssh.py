import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


@dataclass
class SSHTarget:
    host: str
    user: str
    port: int
    private_key: str


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


def inventory_line(alias: str, target: SSHTarget, key_path: str) -> str:
    return (
        f"{alias} ansible_host={target.host} ansible_user={target.user} ansible_port={target.port} "
        f"ansible_ssh_private_key_file={key_path} "
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'"
    )
