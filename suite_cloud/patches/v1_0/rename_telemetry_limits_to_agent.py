from frappe.model.utils.rename_field import rename_field

# The two limits cover every Agent request, telemetry and sending-limit calls alike.


def execute() -> None:
    rename_field("Suite Cloud Settings", "telemetry_requests_per_minute", "agent_requests_per_minute")
    rename_field("Suite Cloud Settings", "telemetry_records_per_request", "agent_records_per_request")
