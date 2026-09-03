app_name = "suite_cloud"
app_title = "Suite Cloud"
app_publisher = "Frappe"
app_description = "Deploys and manages Stalwart mail clusters for Frappe Suite sites"
app_email = "developers@frappe.io"
app_license = "agpl-3.0"

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

# ============================================================================
# Lifecycle
# ============================================================================
after_install = "suite_cloud.install.after_install"
after_migrate = "suite_cloud.install.after_migrate"

# ============================================================================
# Scheduled tasks
# ============================================================================
scheduler_events = {
    "daily": [
        "suite_cloud.suite_cloud.doctype.dns_record.dns_record.verify_all_dns_records",
    ],
}

export_python_type_annotations = True
require_type_annotated_api_methods = True
