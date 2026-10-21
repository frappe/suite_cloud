"""Telemetry alerts: a node that stopped reporting, and a node that logs too many errors.

Every five minutes the checker reads the warehouse, opens a Telemetry Alert for each condition it
finds, resolves the open alerts whose condition cleared, and mails the configured recipients about
both. A Silent Host alert stays open until the node reports again, so the alert of a node taken
out of service is resolved by hand.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, convert_utc_to_system_timezone, get_url_to_form, now_datetime

from suite_cloud.telemetry.warehouse import Warehouse
from suite_cloud.utils import get_config, log_exception

SILENT_HOST = "Silent Host"
ERROR_RATE = "Error Rate"
LOOKBACK = timedelta(hours=24)  # a node that reported this recently is expected to keep reporting
MINIMUM_EVENTS = 100  # an hour with fewer events has no rate worth an alert
DEFAULT_SILENCE_MINUTES = 30  # the settings' defaults, used until the site has them
DEFAULT_ERROR_RATE_PERCENT = 5


def check_telemetry_alerts() -> None:
    """Scheduled every five minutes."""

    Alerts().check()


@dataclass(frozen=True)
class Finding:
    alert_type: str
    host_ip: str
    hostname: str | None
    details: str
    seen_at: datetime | None = None  # a silent host's last batch, in UTC

    @property
    def key(self) -> tuple[str, str]:
        return (self.alert_type, self.host_ip)


class Alerts:
    """The checker: what the warehouse shows now, against the alerts that are open."""

    def __init__(self, warehouse: Warehouse | None = None):
        self.warehouse = warehouse or Warehouse()

    @property
    def silence(self) -> timedelta:
        return timedelta(minutes=cint(get_config("telemetry_silence_minutes")) or DEFAULT_SILENCE_MINUTES)

    @property
    def error_rate_percent(self) -> int:
        return cint(get_config("telemetry_error_rate_percent")) or DEFAULT_ERROR_RATE_PERCENT

    @property
    def recipients(self) -> list[str]:
        text = (get_config("telemetry_alert_recipients") or "").replace("\n", ",")
        return [address.strip() for address in text.split(",") if address.strip()]

    def check(self) -> dict[str, int]:
        """Opens an alert per new finding, resolves the open alerts found no more; returns the counts."""

        open_alerts = {
            (alert.alert_type, alert.host_ip): alert
            for alert in frappe.get_all(
                "Telemetry Alert", {"status": "Open"}, ["name", "alert_type", "host_ip"]
            )
        }
        watched = [key[1] for key in open_alerts if key[0] == SILENT_HOST]
        findings = self.find_silent_hosts(watched) + self.find_error_rates()

        opened = 0
        for finding in findings:
            if alert := open_alerts.pop(finding.key, None):
                frappe.db.set_value("Telemetry Alert", alert.name, "details", finding.details)
            elif not self.is_dismissed(finding):
                self.open(finding)
                opened += 1
        for alert in open_alerts.values():
            self.resolve(alert.name)
        return {"opened": opened, "resolved": len(open_alerts)}

    def is_dismissed(self, finding: Finding) -> bool:
        """A Silent Host alert resolved by hand stays resolved until the node reports again."""

        if finding.alert_type != SILENT_HOST:
            return False
        dismissed_after = convert_utc_to_system_timezone(finding.seen_at).replace(tzinfo=None)
        return bool(
            frappe.db.exists(
                "Telemetry Alert",
                {
                    "alert_type": SILENT_HOST,
                    "host_ip": finding.host_ip,
                    "status": "Resolved",
                    "resolved_at": (">=", dismissed_after),
                },
            )
        )

    def find_silent_hosts(self, watched: list[str]) -> list[Finding]:
        """Nodes that reported within the lookback, or hold an open alert, but not within the
        silence. A node with an open alert stays silent until a newer batch proves it is back:
        past the lookback it keeps its alert, and once the retention has deleted its last rows
        it is still not recovered."""

        now = datetime.now(UTC).replace(tzinfo=None)
        rows = self.warehouse.query(
            'WITH seen AS (SELECT host_ip, hostname, received_at FROM "events" '
            'UNION ALL SELECT host_ip, hostname, received_at FROM "metrics") '
            "SELECT host_ip, arg_max(hostname, received_at), max(received_at) FROM seen "
            "WHERE received_at >= $lookback OR list_contains($watched, host_ip) GROUP BY host_ip",
            {"lookback": now - LOOKBACK, "watched": watched},
        )
        silent = now - self.silence
        findings = [
            Finding(
                SILENT_HOST,
                host_ip,
                hostname,
                _("No batch received since {0} UTC.").format(last.strftime("%Y-%m-%d %H:%M")),
                seen_at=last,
            )
            for host_ip, hostname, last in rows
            if last < silent
        ]
        seen = {host_ip for host_ip, _hostname, _last in rows}
        findings += [
            Finding(SILENT_HOST, host_ip, None, _("No batch received within the retention."))
            for host_ip in watched
            if host_ip not in seen
        ]
        return findings

    def find_error_rates(self) -> list[Finding]:
        """Nodes whose error-level share of the last hour's events is at or over the threshold."""

        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        rows = self.warehouse.query(
            "SELECT host_ip, arg_max(hostname, \"timestamp\"), count(*), count_if(level = 'error') "
            'FROM "events" WHERE "timestamp" >= $since GROUP BY host_ip '
            "HAVING count(*) >= $minimum AND count_if(level = 'error') * 100 >= count(*) * $percent",
            {"since": since, "minimum": MINIMUM_EVENTS, "percent": self.error_rate_percent},
        )
        return [
            Finding(
                ERROR_RATE,
                host_ip,
                hostname,
                _("{0} of {1} events in the last hour are errors ({2}%).").format(
                    errors, total, round(100 * errors / total, 1)
                ),
            )
            for host_ip, hostname, total, errors in rows
        ]

    def open(self, finding: Finding) -> None:
        alert = frappe.get_doc(
            {
                "doctype": "Telemetry Alert",
                "alert_type": finding.alert_type,
                "host_ip": finding.host_ip,
                "hostname": finding.hostname,
                "status": "Open",
                "opened_at": now_datetime(),
                "details": finding.details,
            }
        ).insert(ignore_permissions=True)
        self.notify(alert)

    def resolve(self, name: str) -> None:
        alert = frappe.get_doc("Telemetry Alert", name)
        alert.status = "Resolved"
        alert.details = _("{0} The condition cleared.").format(alert.details)
        alert.save(ignore_permissions=True)
        self.notify(alert)

    def notify(self, alert) -> None:
        """Mails the recipients; a mail that cannot go out is logged, the alert record stands."""

        if not self.recipients:
            return
        state = _("resolved") if alert.status == "Resolved" else _("opened")
        try:
            frappe.sendmail(
                recipients=self.recipients,
                subject=f"[Suite Cloud] {alert.title} {state}",
                message=f"{alert.details}<br><br>{get_url_to_form('Telemetry Alert', alert.name)}",
                reference_doctype="Telemetry Alert",
                reference_name=alert.name,
            )
        except Exception:
            log_exception(f"Telemetry alert mail not sent for {alert.name}", alert)
