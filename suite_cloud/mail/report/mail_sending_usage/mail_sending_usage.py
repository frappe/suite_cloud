# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Accounts by how close they are to their sending limits, from what the Agents last reported for
the current UTC day and month."""

import frappe
from frappe import _
from frappe.utils import flt

from suite_cloud.mail.doctype.mail_send_usage.mail_send_usage import current_periods

NEAR_LIMIT_PERCENT = 80


def execute(filters: dict | None = None) -> tuple[list[dict], list[dict]]:
    filters = frappe._dict(filters or {})
    rows = [usage_row(account) for account in get_accounts(filters)]
    if filters.near_limit:
        rows = [row for row in rows if is_near_limit(row)]
    rows.sort(key=lambda row: (worst_percent(row), row["refused_day"]), reverse=True)
    return get_columns(), rows


def get_accounts(filters: frappe._dict) -> list[frappe._dict]:
    """Accounts with a report for the current periods, each with its day and month rows."""

    day, month = current_periods()
    usage = frappe.qb.DocType("Mail Send Usage")
    account = frappe.qb.DocType("Mail Account")
    query = (
        frappe.qb.from_(usage)
        .join(account)
        .on(usage.account == account.name)
        .select(
            account.name.as_("account"),
            account.tenant,
            account.cluster,
            account.daily_send_limit,
            account.monthly_send_limit,
            usage.period,
            usage.sent,
            usage.refused,
            usage.reported_at,
        )
        .where(usage.period.isin([day, month]))
    )
    for field in ("cluster", "tenant"):
        if filters.get(field):
            query = query.where(account[field] == filters.get(field))

    accounts: dict[str, frappe._dict] = {}
    for row in query.run(as_dict=True):
        entry = accounts.setdefault(row.account, frappe._dict(row, periods={}))
        entry.periods["day" if row.period == day else "month"] = row
        entry.reported_at = max(entry.reported_at, row.reported_at)
    return list(accounts.values())


def usage_row(account: frappe._dict) -> dict:
    day = account.periods.get("day") or frappe._dict(sent=0, refused=0)
    month = account.periods.get("month") or frappe._dict(sent=0, refused=0)
    return {
        "account": account.account,
        "tenant": account.tenant,
        "cluster": account.cluster,
        "sent_today": day.sent,
        "daily_send_limit": account.daily_send_limit,
        "daily_percent": percent(day.sent, account.daily_send_limit),
        "sent_this_month": month.sent,
        "monthly_send_limit": account.monthly_send_limit,
        "monthly_percent": percent(month.sent, account.monthly_send_limit),
        "refused_day": day.refused,
        "refused_month": month.refused,
        "reported_at": account.reported_at,
    }


def percent(sent: int, limit: int) -> float | None:
    """None for an account without the limit, so it sorts and reads as "no limit"."""

    return flt(sent * 100 / limit, 1) if limit else None


def worst_percent(row: dict) -> float:
    return max(row["daily_percent"] or 0, row["monthly_percent"] or 0)


def is_near_limit(row: dict) -> bool:
    return worst_percent(row) >= NEAR_LIMIT_PERCENT or bool(row["refused_day"] or row["refused_month"])


def get_columns() -> list[dict]:
    return [
        {
            "fieldname": "account",
            "label": _("Account"),
            "fieldtype": "Link",
            "options": "Mail Account",
            "width": 240,
        },
        {
            "fieldname": "tenant",
            "label": _("Tenant"),
            "fieldtype": "Link",
            "options": "Suite Tenant",
            "width": 140,
        },
        {
            "fieldname": "cluster",
            "label": _("Cluster"),
            "fieldtype": "Link",
            "options": "Stalwart Cluster",
            "width": 160,
        },
        {"fieldname": "sent_today", "label": _("Sent Today"), "fieldtype": "Int", "width": 100},
        {"fieldname": "daily_send_limit", "label": _("Daily Limit"), "fieldtype": "Int", "width": 100},
        {"fieldname": "daily_percent", "label": _("Daily %"), "fieldtype": "Percent", "width": 90},
        {"fieldname": "sent_this_month", "label": _("Sent This Month"), "fieldtype": "Int", "width": 120},
        {"fieldname": "monthly_send_limit", "label": _("Monthly Limit"), "fieldtype": "Int", "width": 110},
        {"fieldname": "monthly_percent", "label": _("Monthly %"), "fieldtype": "Percent", "width": 90},
        {"fieldname": "refused_day", "label": _("Refused Today"), "fieldtype": "Int", "width": 110},
        {"fieldname": "refused_month", "label": _("Refused This Month"), "fieldtype": "Int", "width": 140},
        {"fieldname": "reported_at", "label": _("Reported At"), "fieldtype": "Datetime", "width": 160},
    ]
