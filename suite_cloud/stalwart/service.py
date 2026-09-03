from collections.abc import Iterator
from typing import Any, ClassVar

from suite_cloud.stalwart.connection import JMAPConnection
from suite_cloud.stalwart.errors import StalwartRejectedError, StalwartUnauthorizedError

CORE_CAPABILITY = "urn:ietf:params:jmap:core"
MANAGEMENT_CAPABILITY = "urn:stalwart:jmap"


class ManagementService:
    """Base for every Stalwart object type reachable through ``x:<Type>/get|set|query``."""

    type: ClassVar[str] = ""
    default_properties: ClassVar[list[str] | None] = None

    def __init__(self, connection: JMAPConnection, type: str | None = None) -> None:
        if type:
            self.type = type
        if not self.type:
            raise NotImplementedError("A ManagementService needs a Stalwart object type.")

        self.connection = connection

    # --- capability limits ------------------------------------------------

    @property
    def account_id(self) -> str:
        """Management calls are scoped to the authenticated account's id."""

        account_id = self.connection.primary_accounts.get(MANAGEMENT_CAPABILITY)
        if not account_id:
            raise StalwartUnauthorizedError(
                f"The session does not advertise {MANAGEMENT_CAPABILITY}; the credentials lack management access.",
                self.type,
            )
        return account_id

    @property
    def max_objects_in_get(self) -> int:
        return (self.connection.capabilities.get(CORE_CAPABILITY) or {}).get("maxObjectsInGet") or 500

    @property
    def max_objects_in_set(self) -> int:
        return (self.connection.capabilities.get(CORE_CAPABILITY) or {}).get("maxObjectsInSet") or 500

    # --- transport ----------------------------------------------------------

    def _method(self, action: str) -> str:
        return f"x:{self.type}/{action}"

    def _call(self, method_calls: list[list]) -> list[dict]:
        """Sends one JMAP request and returns each method response's arguments, in order."""

        response = self.connection.request(
            "POST",
            self.connection.api_url,
            headers={"Content-Type": "application/json"},
            json={"using": [CORE_CAPABILITY, MANAGEMENT_CAPABILITY], "methodCalls": method_calls},
        )

        session_state = response.get("sessionState")
        if session_state and self.connection.state != session_state:
            self.connection.discover()

        results = []
        for method_response in response.get("methodResponses", []):
            name, args = method_response[0], method_response[1]
            if name == "error":
                raise StalwartRejectedError(args, self.type)
            results.append(args)

        return results

    def _invoke(self, action: str, **args) -> dict:
        args = {k: v for k, v in args.items() if v is not None}
        args["accountId"] = self.account_id
        return self._call([[self._method(action), args, "0"]])[0]

    def _set(self, **args) -> dict:
        result = self._invoke("set", **args)
        for key in ("notCreated", "notUpdated", "notDestroyed"):
            if failures := result.get(key):
                raise StalwartRejectedError({key: failures}, self.type)
        return result

    # --- read ---------------------------------------------------------------

    def get(self, id: str, properties: list[str] | None = None) -> dict | None:
        result = self._invoke("get", ids=[id], properties=properties or self.default_properties)
        objects = result.get("list") or []
        return objects[0] if objects else None

    def get_many(self, ids: list[str], properties: list[str] | None = None) -> list[dict]:
        objects: list[dict] = []
        for batch in chunks(ids, self.max_objects_in_get):
            result = self._invoke("get", ids=batch, properties=properties or self.default_properties)
            objects.extend(result.get("list") or [])
        return objects

    def get_all(
        self,
        filter: dict | None = None,
        sort: list[dict] | None = None,
        limit: int | None = None,
        properties: list[str] | None = None,
    ) -> list[dict]:
        """Returns every matching object; a filter chains query and get through a result reference."""

        properties = properties or self.default_properties
        if filter is None and sort is None and limit is None:
            return self._invoke("get", properties=properties).get("list") or []

        query_args: dict[str, Any] = {"accountId": self.account_id}
        query_args.update(
            {k: v for k, v in {"filter": filter, "sort": sort, "limit": limit}.items() if v is not None}
        )
        get_args: dict[str, Any] = {
            "accountId": self.account_id,
            "#ids": {"resultOf": "q", "name": self._method("query"), "path": "/ids"},
        }
        if properties is not None:
            get_args["properties"] = properties

        responses = self._call(
            [[self._method("query"), query_args, "q"], [self._method("get"), get_args, "g"]]
        )
        return responses[1].get("list") or []

    def find(self, filter: dict, properties: list[str] | None = None) -> dict | None:
        matches = self.get_all(filter=filter, limit=1, properties=properties)
        return matches[0] if matches else None

    def find_local(self, **fields) -> dict | None:
        """Matches in Python over all objects; for small config collections whose query filters vary."""

        for obj in self.get_all():
            if all(obj.get(k) == v for k, v in fields.items()):
                return obj
        return None

    def query_ids(self, filter: dict | None = None, limit: int | None = None) -> list[str]:
        result = self._invoke("query", filter=filter, limit=limit)
        return result.get("ids") or []

    # --- write --------------------------------------------------------------

    def create(self, payload: Any) -> dict:
        """Creates one object and returns it including server-set fields (id, secrets...)."""

        return self._set(create={"0": as_payload(payload)})["created"]["0"]

    def create_id(self, payload: Any) -> str:
        return self.create(payload)["id"]

    def update(self, id: str, patch: dict) -> None:
        """Applies a partial update; keys may be property names or JSON-pointer paths."""

        self._set(update={id: patch})

    def update_many(self, patches: dict[str, dict]) -> None:
        for batch in chunk_dict(patches, self.max_objects_in_set):
            self._set(update=batch)

    def delete(self, ids: str | list[str]) -> None:
        ids = [ids] if isinstance(ids, str) else list(ids)
        for batch in chunks(ids, self.max_objects_in_set):
            self._set(destroy=batch)

    def delete_if_exists(self, id: str) -> bool:
        """Deletes ``id``; a missing object is not an error (returns False)."""

        try:
            self.delete(id)
        except StalwartRejectedError as e:
            if e.error_type == "notFound":
                return False
            raise
        return True


class SingletonService(ManagementService):
    """Objects Stalwart keeps exactly one of (SystemSettings, Coordinator, stores, ...)."""

    SINGLETON_ID = "singleton"

    def read(self, properties: list[str] | None = None) -> dict:
        return self.get(self.SINGLETON_ID, properties=properties) or {}

    def write(self, patch: dict) -> None:
        self.update(self.SINGLETON_ID, patch)


def as_payload(payload: Any) -> dict:
    return payload.to_dict() if hasattr(payload, "to_dict") else dict(payload)


def id_set(ids: Any) -> dict[str, bool]:
    """Sets are id-keyed maps on the wire, not arrays."""

    return {str(i): True for i in (ids or [])}


def indexed(items: Any) -> dict[str, dict]:
    """Lists of sub-objects are index-keyed maps on the wire."""

    return {str(i): as_payload(item) for i, item in enumerate(items or [])}


def chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def chunk_dict(d: dict[str, Any], size: int) -> Iterator[dict[str, Any]]:
    keys = list(d)
    for i in range(0, len(keys), size):
        yield {k: d[k] for k in keys[i : i + size]}
