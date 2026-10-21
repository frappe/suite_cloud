import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

# Clusters and gateways made before the Prometheus exporter was configured have no password for it;
# their next Sync Config needs one.
DOCTYPES = ("Stalwart Cluster", "Egress Gateway")


def execute() -> None:
    for doctype in DOCTYPES:
        for name in frappe.get_all(doctype, pluck="name"):
            if get_decrypted_password(doctype, name, "metrics_password", raise_exception=False):
                continue
            set_encrypted_password(doctype, name, frappe.generate_hash(length=32), "metrics_password")
