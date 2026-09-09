import json
from typing import Any


class StalwartError(Exception):
    """A management call failed for a reason other than the server being unreachable.

    ``detail`` carries everything Stalwart said; API layers decide how much of it to expose.
    """

    http_status_code = 502
    kind = "error"

    def __init__(self, detail: Any, object_type: str | None = None) -> None:
        self.detail = detail
        self.object_type = object_type
        super().__init__(self.summary())

    def summary(self) -> str:
        detail = self.detail if isinstance(self.detail, str) else json.dumps(self.detail)
        return f"Stalwart {self.object_type or ''} {self.kind}: {detail}"


class StalwartUnavailableError(StalwartError):
    """The server could not be reached (connection refused, DNS failure, timeout, 502-504).

    ``http_status_code`` makes Frappe answer 503 instead of a generic 500, so API clients can
    tell "the mail server is down" from an application bug.
    """

    http_status_code = 503
    kind = "unavailable"

    def __init__(self, detail: Any = "The mail server is temporarily unavailable.") -> None:
        super().__init__(detail)


class StalwartRejectedError(StalwartError):
    """Stalwart understood the request and refused it (notCreated/notUpdated/notDestroyed).

    ``error_type`` is Stalwart's SetError type (``alreadyExists``, ``invalidProperties``, ...),
    which is safe and useful to relay to callers.
    """

    http_status_code = 422
    kind = "rejected"

    def __init__(self, detail: Any, object_type: str | None = None) -> None:
        self.error_type, self.description = extract_set_error(detail)
        super().__init__(detail, object_type)


class StalwartUnauthorizedError(StalwartError):
    """Our credentials were refused: a configuration problem on the Suite Cloud side."""

    http_status_code = 502
    kind = "unauthorized"


def extract_set_error(detail: Any) -> tuple[str | None, str | None]:
    """Pulls (type, description) out of a SetError, wrapped or not.

    Handles ``{"notCreated": {"0": {"type": ..., "description": ...}}}`` and a bare method-level
    error ``{"type": ..., "description": ...}``.
    """

    if not isinstance(detail, dict):
        return None, None

    if isinstance(detail.get("type"), str):
        return detail["type"], detail.get("description")

    for failures in detail.values():
        if isinstance(failures, dict):
            for failure in failures.values():
                if isinstance(failure, dict) and "type" in failure:
                    return failure.get("type"), failure.get("description")

    return None, None
