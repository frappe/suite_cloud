"""Insights' DuckDB: the loader copies staged batches in, and the retention prunes old rows.

Suite Cloud owns one local DuckDB data source in Insights, with one table per kind of telemetry.
Writes go through Insights' write connection, which holds its file lock, so dashboards reading the
file and this loader never collide: files are checked before the lock is taken, and the lock is
held only for the insert.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import frappe
import pyarrow.parquet as parquet
from frappe import _
from frappe.utils import cint

from suite_cloud.telemetry import TABLES, TelemetryTable
from suite_cloud.telemetry.spool import Spool
from suite_cloud.utils import get_config, log_exception

DATA_SOURCE = "suite_cloud_telemetry"  # the Insights data source and its file under private files
BATCH_FILES = 200  # files loaded under one lock: with 5000 rows each, seconds of hold at most
DEFAULT_RETENTION_DAYS = 90


def load_staged_batches() -> None:
    """Scheduled every five minutes."""

    Warehouse().load_pending()


def prune_expired_rows() -> None:
    """Scheduled daily."""

    Warehouse().prune_expired()


class Warehouse:
    """The data source Suite Cloud's telemetry lives in, and the only writer of its file."""

    def __init__(self, data_source: str = DATA_SOURCE, spool: Spool | None = None):
        self.data_source = data_source
        self.spool = spool or Spool()

    @property
    def retention_days(self) -> int:
        """The configured retention; anything under a day falls back to the default, since a
        negative number would move the cutoff into the future and delete the whole history."""

        days = cint(get_config("telemetry_retention_days"))
        return days if days > 0 else DEFAULT_RETENTION_DAYS

    def ensure_data_source(self) -> None:
        """Install and migrate: the Insights data source, its tables and their registration."""

        if "insights" not in frappe.get_installed_apps():
            frappe.throw(_("Install the insights app before migrating Suite Cloud: it holds the telemetry."))
        if not frappe.db.exists("Insights Data Source v3", self.data_source):
            frappe.get_doc(
                {
                    "doctype": "Insights Data Source v3",
                    "title": frappe.unscrub(self.data_source),  # Insights names the source after it
                    "type": "Database",
                    "database_type": "DuckDB",
                    "database_name": self.data_source,
                }
            ).insert(ignore_permissions=True)

        with self.write_connection() as db:
            for table in TABLES:
                db.raw_sql(table.create_statement)
        self.register_tables()

    def load_pending(self) -> dict[str, int]:
        """Loads the oldest staged files of every table; returns the rows added per table.

        A table whose load fails (the lock timed out, the disk is full) keeps its files for the
        next run and is logged; the other tables still load.
        """

        added = {}
        for table in TABLES:
            files = self.get_loadable(table)
            if not files:
                continue
            try:
                added[table.name] = self.load(table, files)
            except Exception:
                log_exception(f"Telemetry load of {table.name} deferred")
        if added:
            self.register_tables()
        return added

    def get_loadable(self, table: TelemetryTable) -> list[Path]:
        """The staged files that carry the table's schema; the others are moved aside."""

        files = []
        for path in self.spool.get_pending(table, BATCH_FILES):
            try:
                schema = parquet.read_schema(path)
            except Exception as e:
                self.spool.reject(path, str(e))
                continue
            if not schema.equals(table.schema):
                self.spool.reject(path, f"Schema differs from {table.name}:\n{schema}")
                continue
            files.append(path)
        return files

    def load(self, table: TelemetryTable, files: list[Path]) -> int:
        """Inserts the files' rows in one statement and deletes the files once it is committed.

        A batch already in the table is skipped, so a run that died between the commit and the
        delete adds nothing twice. The lookup is bounded to the ids being loaded.
        """

        columns = ", ".join(f'"{column}"' for column in table.columns)
        with self.write_connection() as db:
            db.raw_sql(table.create_statement)
            result = db.con.execute(
                f'INSERT INTO "{table.name}" ({columns}) SELECT {columns} FROM read_parquet($files) '
                f'WHERE batch_id NOT IN (SELECT batch_id FROM "{table.name}" WHERE batch_id IN (SELECT unnest($ids)))',
                {"files": [str(path) for path in files], "ids": [path.stem for path in files]},
            ).fetchone()
        self.spool.delete(files)
        return result[0]

    def prune_expired(self) -> None:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=self.retention_days)
        with self.write_connection() as db:
            for table in TABLES:
                db.raw_sql(table.create_statement)
                db.con.execute(f'DELETE FROM "{table.name}" WHERE timestamp < $cutoff', {"cutoff": cutoff})
            db.raw_sql("CHECKPOINT")

    def write_connection(self):
        return frappe.get_doc("Insights Data Source v3", self.data_source).write_connection()

    def register_tables(self) -> None:
        """Lists the tables in Insights so the query builder offers them; duplicates are ignored."""

        from insights.insights.doctype.insights_table_v3.insights_table_v3 import InsightsTablev3

        InsightsTablev3.bulk_create(self.data_source, [table.name for table in TABLES])
