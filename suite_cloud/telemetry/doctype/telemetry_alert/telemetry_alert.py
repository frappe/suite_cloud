# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""One condition the alert checker (``suite_cloud.telemetry.alerts``) found on a node."""

from frappe.model.document import Document
from frappe.utils import now_datetime


class TelemetryAlert(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        alert_type: DF.Literal["Silent Host", "Error Rate"]
        details: DF.SmallText | None
        host_ip: DF.Data
        hostname: DF.Data | None
        opened_at: DF.Datetime | None
        resolved_at: DF.Datetime | None
        status: DF.Literal["Open", "Resolved"]
        title: DF.Data | None
    # end: auto-generated types

    def validate(self) -> None:
        self.title = f"{self.alert_type}: {self.hostname or self.host_ip}"
        if self.status == "Open":
            self.resolved_at = None
        elif not self.resolved_at:
            self.resolved_at = now_datetime()
