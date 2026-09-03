import frappe

from suite_cloud.api.site import current_site, owned, owned_names, site_api


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def list_domains() -> list[dict]:
    return [
        frappe.get_doc("Mail Domain", name).to_api(with_records=False) for name in owned_names("Mail Domain")
    ]


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_domain(domain: str) -> dict:
    return owned("Mail Domain", domain).to_api()


@frappe.whitelist(methods=["POST"])
@site_api
def create_domain(
    domain: str,
    description: str | None = None,
    catch_all_address: str | None = None,
    sub_addressing: bool = True,
    publish_client_discovery_records: bool = False,
) -> dict:
    doc = frappe.get_doc(
        {
            "doctype": "Mail Domain",
            "domain_name": domain,
            "site": current_site().name,
            "description": description,
            "catch_all_address": catch_all_address,
            "sub_addressing": int(bool(sub_addressing)),
            "publish_client_discovery_records": int(bool(publish_client_discovery_records)),
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.local.response["http_status_code"] = 201
    return doc.to_api()


@frappe.whitelist(methods=["POST", "PUT"])
@site_api
def update_domain(
    domain: str,
    description: str | None = None,
    catch_all_address: str | None = None,
    sub_addressing: bool | None = None,
    publish_client_discovery_records: bool | None = None,
    enabled: bool | None = None,
) -> dict:
    doc = owned("Mail Domain", domain)
    if description is not None:
        doc.description = description
    if catch_all_address is not None:
        doc.catch_all_address = catch_all_address or None
    if sub_addressing is not None:
        doc.sub_addressing = int(bool(sub_addressing))
    if enabled is not None:
        doc.enabled = int(bool(enabled))
    refresh = False
    if publish_client_discovery_records is not None:
        refresh = bool(publish_client_discovery_records) != bool(doc.publish_client_discovery_records)
        doc.publish_client_discovery_records = int(bool(publish_client_discovery_records))
    doc.save(ignore_permissions=True)
    if refresh:
        doc.refresh_dns_records()
    return doc.to_api()


@frappe.whitelist(methods=["POST", "DELETE"])
@site_api
def delete_domain(domain: str) -> None:
    owned("Mail Domain", domain).delete(ignore_permissions=True)


@frappe.whitelist(methods=["GET", "POST"])
@site_api
def get_dns_records(domain: str, refresh: bool = False) -> dict:
    doc = owned("Mail Domain", domain)
    if refresh:
        doc.refresh_dns_records()
    return {
        "domain": doc.domain_name,
        "is_verified": bool(doc.is_verified),
        "records": [r.to_api() for r in doc.dns_records],
    }


@frappe.whitelist(methods=["POST"])
@site_api
def verify_dns_records(domain: str) -> dict:
    return owned("Mail Domain", domain).verify_dns_records()
