# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""What the Agents report an account sent: one row per account and UTC period, named
``{account}:{period}``. The Agents count; Suite Cloud only keeps their last report, so the Mail
Account form never waits on a node."""

from datetime import UTC, datetime, timedelta

import frappe
from frappe.model.document import Document


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


def upsert_usage(rows: list[dict]) -> None:
    """Stores ``{account, period, sent, refused}`` rows in one statement. Agents on several nodes
    report the same counters, and a counter only grows within its period, so the higher count is
    kept."""

    if not rows:
        return
    now = frappe.utils.now()
    user = frappe.session.user
    values = []
    for row in rows:
        values += [f"{row['account']}:{row['period']}", row["account"], row["period"], row["sent"]]
        values += [row["refused"], now, now, now, user, user]
    placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(rows))
    frappe.db.sql(
        f"""insert into `tabMail Send Usage`
            (name, account, period, sent, refused, reported_at, creation, modified, owner, modified_by)
        values {placeholders}
        on duplicate key update
            sent = greatest(sent, values(sent)),
            refused = greatest(refused, values(refused)),
            reported_at = values(reported_at),
            modified = values(modified)""",
        values,
    )


def delete_usage(account: str) -> None:
    frappe.db.delete("Mail Send Usage", {"account": account})


def prune_usage() -> None:
    """Daily: only today, yesterday and their months are kept; nothing older is shown."""

    kept = reportable_periods()
    frappe.db.delete("Mail Send Usage", {"period": ("not in", tuple(kept))})
