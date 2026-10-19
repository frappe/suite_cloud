"""The mail a tenant's site sends, such as invites and password resets: Suite Cloud sends it."""

import frappe
from frappe import _
from frappe.utils import validate_email_address

from suite_cloud.api.tenant import InvalidInputError, current_tenant, tenant_api
from suite_cloud.suite_cloud.doctype.tenant_email import tenant_email


@frappe.whitelist(methods=["POST"])
@tenant_api
def send_email(recipient: str, message: str, request_key: str) -> dict:
    """Sends ``message``, a whole MIME email, to one recipient now and answers whether it was sent."""

    recipient, request_key = (recipient or "").strip(), (request_key or "").strip()
    if "," in recipient or not validate_email_address(recipient):
        frappe.throw(_("{0} is not one email address.").format(recipient), InvalidInputError)
    if not request_key:
        frappe.throw(_("The request_key field is required."), InvalidInputError)
    return tenant_email.send(current_tenant(), recipient, message, request_key).to_api()
