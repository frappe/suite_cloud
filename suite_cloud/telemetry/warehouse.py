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

from suite_cloud.telemetry import DIMENSIONS, MAIL_DOMAINS, NODES, TABLES, TelemetryTable
from suite_cloud.telemetry.spool import Spool
from suite_cloud.utils import get_config, log_exception

DATA_SOURCE = "suite_cloud_telemetry"  # the Insights data source and its file under private files
BATCH_FILES = 200  # files loaded under one lock: with 5000 rows each, seconds of hold at most
DEFAULT_RETENTION_DAYS = 90
NODE_FIELDS = ("hostname", "cluster", "location", "status", "ipv4_address")


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
                self.add_derived_columns(db, table)
        self.refresh_dimensions()
        self.register_tables()

    def add_derived_columns(self, db, table: TelemetryTable) -> None:
        """Adds the derived columns a table created before them lacks, filled from its rows.

        One transaction: a failed backfill leaves the table without the columns, so the next
        migrate tries again.
        """

        existing = {
            name
            for (name,) in db.con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE table_name = $table", {"table": table.name}
            ).fetchall()
        }
        missing = [column for column in table.derived if column.name not in existing]
        if not missing:
            return
        db.con.begin()
        for column in missing:
            db.con.execute(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type}')
        assignments = ", ".join(f'"{column.name}" = {column.expression}' for column in missing)
        db.con.execute(f'UPDATE "{table.name}" SET {assignments}')
        db.con.commit()

    def load_pending(self) -> dict[str, int]:
        """Loads every staged file of every table, oldest first; returns the rows added per table.

        Files go in chunks of BATCH_FILES, each under its own lock, so a dashboard query gets its
        turn between chunks and a backlog drains in one run. A table whose load fails (the lock
        timed out, the disk is full) keeps its files for the next run and is logged; the other
        tables still load.
        """

        added = {}
        for table in TABLES:
            while files := self.get_loadable(table):
                try:
                    added[table.name] = added.get(table.name, 0) + self.load(table, files)
                except Exception:
                    log_exception(f"Telemetry load of {table.name} deferred")
                    break
                if len(files) < BATCH_FILES:
                    break
        try:
            self.refresh_dimensions()
        except Exception:
            log_exception("Telemetry lookup tables not refreshed")
        if added:
            self.register_tables()
        return added

    def refresh_dimensions(self) -> None:
        """Rewrites the node and mail domain tables from Suite Cloud's records.

        The records are read before the lock is taken, so the file is held only for two small
        table swaps.
        """

        batches = {
            NODES: NODES.get_batch({}, get_node_rows()),
            MAIL_DOMAINS: MAIL_DOMAINS.get_batch({}, get_mail_domain_rows()),
        }
        with self.write_connection() as db:
            for table, batch in batches.items():
                db.con.register("dimension", batch)
                db.con.execute(f'CREATE OR REPLACE TABLE "{table.name}" AS SELECT * FROM dimension')
                db.con.unregister("dimension")

    def get_loadable(self, table: TelemetryTable) -> list[Path]:
        """The next chunk of staged files that read whole and carry the table's schema.

        Each file is read in full here, outside the lock: a damaged page found inside the insert
        would fail the whole chunk on every run, so it is moved aside instead.
        """

        files = []
        for path in self.spool.get_pending(table, BATCH_FILES):
            try:
                schema = parquet.read_table(path).schema
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

        The derived columns are computed here, once per row. A batch already in the table is
        skipped, so a run that died between the commit and the delete adds nothing twice. The
        lookup is bounded to the ids being loaded.
        """

        names = [*table.columns, *(column.name for column in table.derived)]
        values = [f'"{column}"' for column in table.columns]
        values += [f'{column.expression} AS "{column.name}"' for column in table.derived]
        columns = ", ".join(f'"{name}"' for name in names)
        with self.write_connection() as db:
            db.raw_sql(table.create_statement)
            result = db.con.execute(
                f'INSERT INTO "{table.name}" ({columns}) SELECT {", ".join(values)} FROM read_parquet($files) '
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

    def query(self, statement: str, parameters: dict) -> list[tuple]:
        """A read under the writer's lock: a job must leave no reader open, since one open in a
        worker would block the next load."""

        with self.write_connection() as db:
            return db.con.execute(statement, parameters).fetchall()

    def write_connection(self):
        return frappe.get_doc("Insights Data Source v3", self.data_source).write_connection()

    def register_tables(self) -> None:
        """Lists the tables in Insights so the query builder offers them; duplicates are ignored."""

        from insights.insights.doctype.insights_table_v3.insights_table_v3 import InsightsTablev3

        InsightsTablev3.bulk_create(self.data_source, [table.name for table in TABLES + DIMENSIONS])


def get_node_rows() -> list[dict]:
    """One row per address of every Stalwart Node and Egress Gateway, since a batch names its host
    by address. A gateway runs a Stalwart of its own that sends the cluster's mail, so the
    dashboards see it as a node of the cluster with the role ``gateway``."""

    rows = []
    for node in frappe.get_all("Stalwart Node", fields=[*NODE_FIELDS, "role", "ipv6_address"]):
        rows += get_host_rows(node, node.role, (node.ipv4_address, node.ipv6_address))
    for gateway in frappe.get_all("Egress Gateway", fields=NODE_FIELDS):
        rows += get_host_rows(gateway, "gateway", (gateway.ipv4_address,))
    return rows


def get_host_rows(record: dict, role: str, addresses: tuple[str | None, ...]) -> list[dict]:
    return [
        {
            "host_ip": address,
            "hostname": record.hostname,
            "cluster": record.cluster,
            "role": role,
            "location": record.location,
            "status": record.status,
        }
        for address in addresses
        if address
    ]


def get_mail_domain_rows() -> list[dict]:
    titles = dict(frappe.get_all("Suite Tenant", fields=["name", "title"], as_list=True))
    return [
        {"domain": domain.name, "tenant": domain.tenant, "tenant_title": titles.get(domain.tenant)}
        for domain in frappe.get_all("Mail Domain", fields=["name", "tenant"])
    ]
