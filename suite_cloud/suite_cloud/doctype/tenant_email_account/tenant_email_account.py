# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TenantEmailAccount(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        email_account: DF.Link
        parent: DF.Data
        parentfield: DF.Data
        parenttype: DF.Data
        tenant_type: DF.Literal["Suite Site", "Team", "Development"]
    # end: auto-generated types

    pass
