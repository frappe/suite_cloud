from frappe.model.utils.rename_field import rename_field


def execute() -> None:
    """The two limits cover every Agent request, telemetry and sending-limit calls alike."""

    rename_field("Suite Cloud Settings", "telemetry_requests_per_minute", "agent_requests_per_minute")
    rename_field("Suite Cloud Settings", "telemetry_records_per_request", "agent_records_per_request")
