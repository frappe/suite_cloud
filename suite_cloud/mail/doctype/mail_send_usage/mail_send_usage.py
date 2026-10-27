# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What the Agents report an account sent: one row per account and UTC period, named
``{account}:{period}``. The Agents count; Suite Cloud keeps their last report of each period, for
the retention in Suite Cloud Settings, so the Mail Account form never waits on a node."""

from datetime import UTC, datetime, timedelta

import frappe
from frappe.model.document import Document
from frappe.query_builder import CustomFunction
from frappe.utils import cint
from pypika.terms import Values

from suite_cloud.utils import get_config

DEFAULT_RETENTION_DAYS = 90
Greatest = CustomFunction("GREATEST", ["first", "second"])


class MailSendUsage(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        account: DF.Link
        period: DF.Data
        refused: DF.Int
        reported_at: DF.Datetime | None
        send_limit: DF.Int
        sent: DF.Int
    # end: auto-generated types

    pass


def periods_at(moment: datetime) -> tuple[str, str]:
    """The day and month keys the Agents count under, in UTC."""

    moment = moment.astimezone(UTC)
    return f"d:{moment:%Y%m%d}", f"m:{moment:%Y%m}"


def current_periods() -> tuple[str, str]:
    return periods_at(datetime.now(UTC))


def reportable_periods() -> set[str]:
    """Today, this month and the ones before: an Agent may push just after midnight."""

    now = datetime.now(UTC)
    return {*periods_at(now), *periods_at(now - timedelta(days=1))}


def sent_by_account(accounts: list[str]) -> dict[str, dict[str, int]]:
    """``{account: {day, month, refused_day, refused_month}}`` for the current periods: recipients
    sent and messages refused, in one query."""

    day, month = current_periods()
    usage = {account: {"day": 0, "month": 0, "refused_day": 0, "refused_month": 0} for account in accounts}
    if not accounts:
        return usage
    rows = frappe.get_all(
        "Mail Send Usage",
        filters={"account": ("in", accounts), "period": ("in", (day, month))},
        fields=["account", "period", "sent", "refused"],
    )
    for row in rows:
        key = "day" if row.period == day else "month"
        usage[row.account][key] = row.sent
        usage[row.account][f"refused_{key}"] = row.refused
    return usage


def usage_history(account: str, days: int) -> dict[str, list[dict]]:
    """The account's totals of the last ``days`` UTC days and of their months, newest first, each
    with the limit it was last reported with."""

    day, month = periods_at(datetime.now(UTC) - timedelta(days=days - 1))
    usage = frappe.qb.DocType("Mail Send Usage")
    rows = (
        frappe.qb.from_(usage)
        .select(usage.period, usage.sent, usage.refused, usage.send_limit)
        .where(usage.account == account)
        .where(
            (usage.period.like("d:%") & (usage.period >= day))
            | (usage.period.like("m:%") & (usage.period >= month))
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
        history[key].append({**entry, "sent": row.sent, "refused": row.refused, "limit": row.send_limit})
    return history


def upsert_usage(rows: list[dict]) -> None:
    """Stores ``{account, period, sent, refused}`` rows in one statement, each with the account's
    limit for its period as it is now. Agents on several nodes report the same counters, and a
    counter only grows within its period, so the higher count is kept."""

    if not rows:
        return
    limits = limits_by_account(list({row["account"] for row in rows}))
    now = frappe.utils.now()
    user = frappe.session.user
    usage = frappe.qb.DocType("Mail Send Usage")
    query = frappe.qb.into(usage).columns(
        usage.name,
        usage.account,
        usage.period,
        usage.sent,
        usage.refused,
        usage.send_limit,
        usage.reported_at,
        usage.creation,
        usage.modified,
        usage.owner,
        usage.modified_by,
    )
    for row in rows:
        name = f"{row['account']}:{row['period']}"
        limit = limits[row["account"]][row["period"][0]]
        query = query.insert(
            name, row["account"], row["period"], row["sent"], row["refused"], limit, now, now, now, user, user
        )
    query = (
        query.on_duplicate_key_update(usage.sent, Greatest(usage.sent, Values(usage.sent)))
        .on_duplicate_key_update(usage.refused, Greatest(usage.refused, Values(usage.refused)))
        .on_duplicate_key_update(usage.send_limit, Values(usage.send_limit))
        .on_duplicate_key_update(usage.reported_at, Values(usage.reported_at))
        .on_duplicate_key_update(usage.modified, Values(usage.modified))
    )
    query.run()


def limits_by_account(accounts: list[str]) -> dict[str, dict[str, int]]:
    """``{account: {"d": daily, "m": monthly}}``, keyed like the first letter of a period."""

    rows = frappe.get_all(
        "Mail Account",
        filters={"name": ("in", accounts)},
        fields=["name", "daily_send_limit", "monthly_send_limit"],
    )
    return {row.name: {"d": row.daily_send_limit, "m": row.monthly_send_limit} for row in rows}


def delete_usage(account: str) -> None:
    frappe.db.delete("Mail Send Usage", {"account": account})


def prune_usage() -> None:
    """Daily: drops the days older than the retention and the months that ended before it."""

    day, month = periods_at(datetime.now(UTC) - timedelta(days=retention_days()))
    usage = frappe.qb.DocType("Mail Send Usage")
    old_days = usage.period.like("d:%") & (usage.period < day)
    old_months = usage.period.like("m:%") & (usage.period < month)
    frappe.qb.from_(usage).delete().where(old_days | old_months).run()


def retention_days() -> int:
    """The configured retention; anything under a day falls back to the default, since a negative
    number would move the cutoff into the future and delete today's counts."""

    days = cint(get_config("send_usage_retention_days"))
    return days if days > 0 else DEFAULT_RETENTION_DAYS
