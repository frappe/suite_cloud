"""Memory lookup keys written by id. Stalwart cannot find a key by its name, so each account keeps
its key's id. A namespace and name are unique across both kinds of key, and Stalwart names the
holder when a create finds them taken, so a lost id is found again without reading the namespace."""

from suite_cloud.mail.stalwart.errors import StalwartRejectedError
from suite_cloud.mail.stalwart.service import ManagementService


def create_key(service: ManagementService, payload: dict) -> tuple[str, bool]:
    """The new key's id and True, or the id of the key already holding the name and False."""

    try:
        return service.create(payload)["id"], True
    except StalwartRejectedError as error:
        if taken := taken_key_id(error, service.type):
            return taken, False
        raise


def delete_key(service: ManagementService, key_id: str) -> None:
    try:
        service.delete([key_id])
    except StalwartRejectedError as error:
        if error.error_type != "notFound":  # gone already
            raise


def taken_key_id(error: StalwartRejectedError, object_type: str) -> str | None:
    """The id a ``primaryKeyViolation`` names, when it is a key of the same kind; a key of the
    other kind with the same name is another feature's, and stays refused."""

    for failure in (error.detail.get("notCreated") or {}).values():
        taken = failure.get("objectId") or {}
        if failure.get("type") == "primaryKeyViolation" and taken.get("object") == object_type:
            return taken.get("id")
    return None
