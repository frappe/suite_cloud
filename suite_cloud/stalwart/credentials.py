"""Account credentials: app passwords (IMAP/JMAP logins) and API keys (Bearer tokens).

Both are account-scoped objects: the connection must be authenticated as the account they
belong to (the admin account for Suite Cloud's own key, a master-user login for members).
"""

from dataclasses import dataclass
from typing import ClassVar

from suite_cloud.stalwart.errors import StalwartError
from suite_cloud.stalwart.service import ManagementService, id_set


@dataclass
class Credential:
    description: str
    permissions: dict | None = None
    expires_at: str | None = None
    allowed_ips: list[str] | None = None

    def to_dict(self) -> dict:
        payload = {"description": self.description, "permissions": self.permissions or {"@type": "Inherit"}}
        if self.expires_at:
            payload["expiresAt"] = self.expires_at
        if self.allowed_ips:
            payload["allowedIps"] = list(self.allowed_ips)
        return payload


def replace_permissions(enabled: list[str]) -> dict:
    """A least-privilege credential: exactly these permissions, nothing inherited."""

    return {"@type": "Replace", "enabledPermissions": id_set(enabled), "disabledPermissions": {}}


class CredentialService(ManagementService):
    default_properties: ClassVar[list[str]] = ["id", "description", "createdAt", "expiresAt"]

    def create_secret(self, credential: Credential) -> tuple[str, str]:
        """Creates the credential and returns (id, secret); the secret is only ever shown here."""

        created = self.create(credential)
        secret = created.get("secret")
        if not secret:
            raise StalwartError("The server did not return the generated secret.", self.type)
        return created["id"], secret


class AppPasswordService(CredentialService):
    type = "AppPassword"


class ApiKeyService(CredentialService):
    type = "ApiKey"
