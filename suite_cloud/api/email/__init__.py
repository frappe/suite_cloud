"""The emails a tenant's site sends about Suite, such as invites and password resets: Suite Cloud
holds their templates and sends them."""

import frappe
from frappe import _
from frappe.utils import validate_email_address

from suite_cloud.api.tenant import InvalidInputError, current_tenant, tenant_api
from suite_cloud.suite_cloud.doctype.tenant_email import tenant_email


@frappe.whitelist(methods=["POST"])
@tenant_api
def send_email(email_type: str, recipient: str, params: dict, request_key: str) -> dict:
    """Sends one email of ``email_type`` to one recipient now and answers whether it was sent."""

    recipient, request_key = (recipient or "").strip(), (request_key or "").strip()
    if "," in recipient or not validate_email_address(recipient):
        frappe.throw(_("{0} is not one email address.").format(recipient), InvalidInputError)
    if not request_key:
        frappe.throw(_("The request_key field is required."), InvalidInputError)
    tenant = current_tenant()
    return tenant_email.send(tenant, email_type, recipient, frappe.parse_json(params), request_key).to_api()
