# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What an account sent: one row per account and UTC period, unique on the two, for billing.
``suite_cloud.mail.send_usage`` adds the recipients of each message Stalwart queued; rows are
kept for the retention in Suite Cloud Settings."""

import hashlib
from datetime import UTC, datetime, timedelta

import frappe
from frappe.model.document import Document
from frappe.query_builder import Case
from frappe.utils import cint
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
        period: DF.Data
        reported_at: DF.Datetime | None
        send_limit: DF.Int
        sent: DF.Int
    # end: auto-generated types

    pass


def on_doctype_update() -> None:
    """The upsert finds a period's row by this key."""

    frappe.db.add_unique("Mail Send Usage", ["account", "period"], constraint_name="account_period")


def usage_name(account: str, period: str) -> str:
    """A fixed-length name, since an account's name and a period together can pass the name column."""

    return hashlib.sha1(f"{account}:{period}".encode()).hexdigest()


def periods_at(moment: datetime) -> tuple[str, str]:
    """The day and month keys the counters use, in UTC."""

    moment = moment.astimezone(UTC)
    return f"d:{moment:%Y%m%d}", f"m:{moment:%Y%m}"


def current_periods() -> tuple[str, str]:
    return periods_at(datetime.now(UTC))


def sent_by_account(accounts: list[str]) -> dict[str, dict[str, int]]:
    """``{account: {day, month}}``: recipients sent in the current periods, in one query."""

    day, month = current_periods()
    usage = {account: {"day": 0, "month": 0} for account in accounts}
    if not accounts:
        return usage
    rows = frappe.get_all(
        "Mail Send Usage",
        filters={"account": ("in", accounts), "period": ("in", (day, month))},
        fields=["account", "period", "sent"],
    )
    for row in rows:
        usage[row.account]["day" if row.period == day else "month"] = row.sent
    return usage


def usage_history(account: str, days: int) -> dict[str, list[dict]]:
    """The account's totals of the last ``days`` UTC days, today included, and of their months,
    newest first, each with the daily limit the account had while the day was current."""

    first_day, first_month = periods_at(datetime.now(UTC) - timedelta(days=days - 1))
    today, this_month = current_periods()
    usage = frappe.qb.DocType("Mail Send Usage")
    rows = (
        frappe.qb.from_(usage)
        .select(usage.period, usage.sent, usage.send_limit)
        .where(usage.account == account)
        .where(
            (usage.period.like("d:%") & usage.period[first_day:today])
            | (usage.period.like("m:%") & usage.period[first_month:this_month])
        )
        .orderby(usage.period, order=frappe.qb.desc)
        .run(as_dict=True)
    )
    history = {"days": [], "months": []}
    for row in rows:
        stamp = row.period[2:]
        if row.period.startswith("d:"):
            entry, key = {"date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"}, "days"
        else:
            entry, key = {"month": f"{stamp[:4]}-{stamp[4:]}"}, "months"
        if key == "days":
            entry["limit"] = row.send_limit
        history[key].append({**entry, "sent": row.sent})
    return history


def add_usage(totals: dict[tuple[str, str], int]) -> int:
    """Adds ``{(account, period): recipients}`` to the stored rows in one statement and returns how
    many rows it wrote. A current period takes the account's daily limit as it is now; a past one
    keeps the limit it was stored with. A month has no limit, so it stores 0."""

    limits = daily_limits(list({account for account, _period in totals}))
    rows = sorted((key, sent) for key, sent in totals.items() if key[0] in limits)  # deleted meanwhile
    if not rows:
        return 0
    # One lock order for every writer, so two batches of the same accounts do not deadlock.
    now = frappe.utils.now()
    user = frappe.session.user
    usage = frappe.qb.DocType("Mail Send Usage")
    limit_while_current = (
        Case().when(usage.period.isin(current_periods()), Values(usage.send_limit)).else_(usage.send_limit)
    )
    query = frappe.qb.into(usage).columns(
        usage.name,
        usage.account,
        usage.period,
        usage.sent,
        usage.send_limit,
        usage.reported_at,
        usage.creation,
        usage.modified,
        usage.owner,
        usage.modified_by,
    )
    for (account, period), sent in rows:
        limit = limits[account] if period.startswith("d:") else 0
        query = query.insert(
            usage_name(account, period), account, period, sent, limit, now, now, now, user, user
        )
    query = (
        query.on_duplicate_key_update(usage.sent, usage.sent + Values(usage.sent))
        .on_duplicate_key_update(usage.send_limit, limit_while_current)
        .on_duplicate_key_update(usage.reported_at, Values(usage.reported_at))
        .on_duplicate_key_update(usage.modified, Values(usage.modified))
    )
    query.run()
    return len(rows)


def daily_limits(accounts: list[str]) -> dict[str, int]:
    return dict(
        frappe.get_all(
            "Mail Account",
            filters={"name": ("in", accounts)},
            fields=["name", "daily_send_limit"],
            as_list=True,
        )
    )


def delete_usage(account: str) -> None:
    frappe.db.delete("Mail Send Usage", {"account": account})


def prune_usage_job() -> None:
    """Scheduled daily, and the only place that commits: one expired period per transaction, so a
    shortened retention never deletes months of rows at once."""

    for period in get_expired_periods():
        frappe.db.delete("Mail Send Usage", {"period": period})
        if not frappe.in_test:
            frappe.db.commit()


def get_expired_periods() -> list[str]:
    """The days older than the retention and the months that ended before it, oldest first."""

    day, month = periods_at(datetime.now(UTC) - timedelta(days=retention_days()))
    usage = frappe.qb.DocType("Mail Send Usage")
    old_days = usage.period.like("d:%") & (usage.period < day)
    old_months = usage.period.like("m:%") & (usage.period < month)
    return sorted(
        frappe.qb.from_(usage).select(usage.period).distinct().where(old_days | old_months).run(pluck=True)
    )


def retention_days() -> int:
    """The configured retention; anything under a day falls back to the default, since a negative
    number would move the cutoff into the future and delete today's counts."""

    days = cint(get_config("send_usage_retention_days"))
    return days if days > 0 else DEFAULT_RETENTION_DAYS
