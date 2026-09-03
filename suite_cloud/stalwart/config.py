"""Server configuration objects: actions, cluster registry and the config singletons."""

from typing import ClassVar

from suite_cloud.stalwart.service import ManagementService

# Object types Stalwart keeps exactly one of; plans update them without an id.
SINGLETON_TYPES = frozenset(
    {
        "Bootstrap",
        "SystemSettings",
        "Coordinator",
        "DataStore",
        "BlobStore",
        "SearchStore",
        "InMemoryStore",
        "MtaOutboundStrategy",
        "Enterprise",
        "Http",
        "Jmap",
        "Imap",
        "OidcProvider",
        "Security",
        "Cache",
        "TaskManager",
    }
)


class ActionService(ManagementService):
    """Actions are not queryable: running one is a set/create whose created object holds the result."""

    type = "Action"

    def run(self, action_type: str, **params) -> dict:
        return self.create({"@type": action_type, **params})


class ClusterNodeService(ManagementService):
    """The read-only registry of node-id leases (nodeId, hostname, lastRenewal, status)."""

    type = "ClusterNode"
    default_properties: ClassVar[list[str]] = ["id", "nodeId", "hostname", "lastRenewal", "status"]

    def find_by_hostname(self, hostname: str) -> dict | None:
        return self.find_local(hostname=hostname)


class ClusterRoleService(ManagementService):
    type = "ClusterRole"
    default_properties: ClassVar[list[str]] = ["id", "name", "description", "tasks", "listeners"]

    def find_by_name(self, name: str) -> dict | None:
        return self.find_local(name=name)
