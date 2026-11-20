# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What an account sent: one row per account and UTC day, unique on the two, for billing; a month
is the sum of its days. A row keeps the account's tenant and outlives the account. ``suite_cloud.mail.send_usage`` adds the recipients of each message Stalwart
queued; rows are kept for the retention in Suite Cloud Settings."""

import hashlib
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

import frappe
from frappe.model.document import Document
from frappe.query_builder import Case
from frappe.query_builder.functions import Sum
from frappe.utils import add_months, cint
from pypika.terms import Values

from suite_cloud.utils import get_config

DEFAULT_RETENTION_DAYS = 90


class MailSendUsage(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        account: DF.Link
        date: DF.Date
        reported_at: DF.Datetime | None
        send_limit: DF.Int
        sent: DF.Int
        tenant: DF.Link | None
    # end: auto-generated types

    pass


def on_doctype_update() -> None:
    """The upsert finds a day's row by this key."""

    frappe.db.add_unique("Mail Send Usage", ["account", "date"], constraint_name="account_date")


def usage_name(account: str, day: date) -> str:
    """A fixed-length name, since an account's name and a date together can pass the name column."""

    return hashlib.sha1(f"{account}:{day.isoformat()}".encode()).hexdigest()


def utc_date(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


def utc_today() -> date:
    return utc_date(datetime.now(UTC))


def sent_by_account(accounts: list[str]) -> dict[str, dict[str, int]]:
    """``{account: {day, month}}``: recipients sent today and this month, UTC, in one query."""

    usage = {account: {"day": 0, "month": 0} for account in accounts}
    if not accounts:
        return usage
    today = utc_today()
    rows = frappe.get_all(
        "Mail Send Usage",
        filters={"account": ("in", accounts), "date": (">=", today.replace(day=1))},
        fields=["account", "date", "sent"],
    )
    for row in rows:
        usage[row.account]["month"] += row.sent
        if row.date == today:
            usage[row.account]["day"] = row.sent
    return usage


def usage_history(account: str, days: int) -> dict[str, list[dict]]:
    """The account's totals of the last ``days`` UTC days, today included, each with the daily limit
    it had that day, and of their months, newest first. A month sums every day stored for it."""

    today = utc_today()
    first_day = today - timedelta(days=days - 1)
    rows = frappe.get_all(
        "Mail Send Usage",
        filters={"account": account, "date": ("between", (first_day.replace(day=1), today))},
        fields=["date", "sent", "send_limit"],
        order_by="date desc",
    )
    months = defaultdict(int)
    for row in rows:
        months[f"{row.date:%Y-%m}"] += row.sent
    return {
        "days": [
            {"date": row.date.isoformat(), "sent": row.sent, "limit": row.send_limit}
            for row in rows
            if row.date >= first_day
        ],
        "months": [{"month": month, "sent": sent} for month, sent in months.items()],
    }


def tenant_usage_by_month(tenant: str, months: int) -> list[dict]:
    """The tenant's recipients per UTC month for the last ``months``, this one included, newest
    first: each month sums every day kept for the tenant's accounts, deleted ones included."""

    first = add_months(utc_today().replace(day=1), -(months - 1))
    usage = frappe.qb.DocType("Mail Send Usage")
    rows = (
        frappe.qb.from_(usage)
        .select(usage.date, Sum(usage.sent).as_("sent"))
        .where((usage.tenant == tenant) & (usage.date >= first))
        .groupby(usage.date)
        .run(as_dict=True)
    )
    totals = defaultdict(int)
    for row in rows:
        totals[f"{row.date:%Y-%m}"] += int(row.sent)
    return [{"month": month, "sent": totals[month]} for month in sorted(totals, reverse=True)]


def add_usage(totals: dict[tuple[str, date], int]) -> int:
    """Adds ``{(account, day): recipients}`` to the stored rows in one statement and returns how
    many rows it wrote. Today's row takes the account's daily limit as it is now; a past day keeps
    the limit it was stored with."""

    accounts = get_accounts(list({account for account, _day in totals}))
    rows = sorted((key, sent) for key, sent in totals.items() if key[0] in accounts)  # deleted meanwhile
    if not rows:
        return 0
    # One lock order for every writer, so two batches of the same accounts do not deadlock.
    now = frappe.utils.now()
    user = frappe.session.user
    usage = frappe.qb.DocType("Mail Send Usage")
    limit_while_current = (
        Case().when(usage.date == utc_today(), Values(usage.send_limit)).else_(usage.send_limit)
    )
    query = frappe.qb.into(usage).columns(
        usage.name,
        usage.account,
        usage.tenant,
        usage.date,
        usage.sent,
        usage.send_limit,
        usage.reported_at,
        usage.creation,
        usage.modified,
        usage.owner,
        usage.modified_by,
    )
    for (account, day), sent in rows:
        limit, tenant = accounts[account]
        query = query.insert(
            usage_name(account, day), account, tenant, day, sent, limit, now, now, now, user, user
        )
    query = (
        query.on_duplicate_key_update(usage.sent, usage.sent + Values(usage.sent))
        .on_duplicate_key_update(usage.send_limit, limit_while_current)
        .on_duplicate_key_update(usage.reported_at, Values(usage.reported_at))
        .on_duplicate_key_update(usage.modified, Values(usage.modified))
    )
    query.run()
    return len(rows)


def get_accounts(accounts: list[str]) -> dict[str, tuple[int, str]]:
    """``{account: (daily limit, tenant)}``."""

    rows = frappe.get_all(
        "Mail Account", filters={"name": ("in", accounts)}, fields=["name", "daily_send_limit", "tenant"]
    )
    return {row.name: (row.daily_send_limit, row.tenant) for row in rows}


def prune_usage_job() -> None:
    """Scheduled daily, and the only place that commits: one expired day per transaction, so a
    shortened retention never deletes months of rows at once."""

    cutoff = utc_today() - timedelta(days=retention_days())
    expired = frappe.get_all(
        "Mail Send Usage", filters={"date": ("<", cutoff)}, pluck="date", distinct=True, order_by="date"
    )
    for day in expired:
        frappe.db.delete("Mail Send Usage", {"date": day})
        if not frappe.in_test:
            frappe.db.commit()


def retention_days() -> int:
    """The configured retention; anything under a day falls back to the default, since a negative
    number would move the cutoff into the future and delete today's counts."""

    days = cint(get_config("send_usage_retention_days"))
    return days if days > 0 else DEFAULT_RETENTION_DAYS
