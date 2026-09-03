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
    "cron": {
        "*/5 * * * *": [
            "suite_cloud.suite_cloud.doctype.server_job.server_job.retry_failed_jobs",
            "suite_cloud.suite_cloud.doctype.stalwart_node.stalwart_node.poll_pending_nodes",
            "suite_cloud.suite_cloud.doctype.egress_gateway.egress_gateway.poll_pending_gateways",
        ],
    },
    "hourly": [
        "suite_cloud.suite_cloud.doctype.mail_domain.mail_domain.refresh_rotating_domains",
        "suite_cloud.suite_cloud.doctype.mail_domain.mail_domain.verify_unverified_domains",
    ],
    "daily": [
        "suite_cloud.suite_cloud.doctype.dns_record.dns_record.verify_all_dns_records",
        "suite_cloud.suite_cloud.doctype.mail_domain.mail_domain.refresh_all_domains",
        "suite_cloud.suite_cloud.doctype.stalwart_cluster.stalwart_cluster.check_all_clusters",
        "suite_cloud.suite_cloud.doctype.stalwart_node.stalwart_node.verify_all_ptr_records",
    ],
}

# Execution history points back at its server; it must not block deleting the server.
ignore_links_on_delete = ["Server Job"]

export_python_type_annotations = True
require_type_annotated_api_methods = True
