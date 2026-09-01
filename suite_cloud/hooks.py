app_name = "suite_cloud"
app_title = "Suite Cloud"
app_publisher = "Frappe"
app_description = (
    "Deploys and manages the infrastructure behind Frappe Suite: mail servers today, more services to come"
)
app_email = "developers@frappe.io"
app_license = "agpl-3.0"

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

# ============================================================================
# Lifecycle
# ============================================================================
before_install = "suite_cloud.install.before_install"
after_install = "suite_cloud.install.after_install"

# ============================================================================
# Scheduled tasks
# ============================================================================
scheduler_events = {
    "cron": {
        "*/5 * * * *": [
            "suite_cloud.suite_cloud.doctype.server_job.server_job.retry_failed_jobs",
            "suite_cloud.suite_cloud.doctype.server_deployment.server_deployment.retry_failed_deployments",
            "suite_cloud.suite_cloud.doctype.server_ansible_play.server_ansible_play.retry_failed_ansible_plays",
        ],
    },
}

# Execution history points back at its Mail Server; it must not block deleting the server.
ignore_links_on_delete = [
    "Server Job",
    "Server Ansible Play",
    "Server Deployment",
]

export_python_type_annotations = True
require_type_annotated_api_methods = True
