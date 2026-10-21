"""Logs, traces and metrics the agents on Stalwart and SFU nodes push to Suite Cloud.

A batch arrives as JSON, is staged as a parquet file by the ``Spool`` and is copied into Frappe
Insights' DuckDB by the ``Warehouse`` loader, where the shipped dashboards read it. The tables
here are the one definition of the columns: the parquet schema, the DuckDB DDL and the insert
column list all come from them.
"""

from dataclasses import dataclass

import pyarrow

TELEMETRY_USER = "telemetry@suite-cloud.internal"  # the agents' bearer secret signs this user in

DUCKDB_TYPES = {
    pyarrow.string(): "VARCHAR",
    pyarrow.timestamp("us"): "TIMESTAMP",
    pyarrow.float64(): "DOUBLE",
    pyarrow.int64(): "BIGINT",
}

# What every row of one batch shares. Timestamps are naive UTC by contract.
ENVELOPE_FIELDS = (
    pyarrow.field("batch_id", pyarrow.string(), nullable=False),
    pyarrow.field("received_at", pyarrow.timestamp("us"), nullable=False),
    pyarrow.field("source", pyarrow.string(), nullable=False),
    pyarrow.field("host_ip", pyarrow.string(), nullable=False),
    pyarrow.field("hostname", pyarrow.string()),
)


@dataclass(frozen=True)
class TelemetryTable:
    """One kind of telemetry: its DuckDB table, its spool folder and its columns."""

    name: str
    schema: pyarrow.Schema

    @property
    def columns(self) -> list[str]:
        return self.schema.names

    @property
    def create_statement(self) -> str:
        columns = ", ".join(
            f'"{field.name}" {DUCKDB_TYPES[field.type]}{"" if field.nullable else " NOT NULL"}'
            for field in self.schema
        )
        return f'CREATE TABLE IF NOT EXISTS "{self.name}" ({columns})'

    def get_batch(self, envelope: dict, rows: list[dict]) -> pyarrow.Table:
        """The rows of one request as a parquet-ready table, each carrying the envelope."""

        return pyarrow.Table.from_pylist([{**envelope, **row} for row in rows], schema=self.schema)


EVENTS = TelemetryTable(
    "events",
    pyarrow.schema(
        [
            *ENVELOPE_FIELDS,
            pyarrow.field("timestamp", pyarrow.timestamp("us"), nullable=False),
            pyarrow.field("level", pyarrow.string(), nullable=False),
            pyarrow.field("event", pyarrow.string(), nullable=False),
            pyarrow.field("span_id", pyarrow.string()),
            pyarrow.field("data", pyarrow.string()),
        ]
    ),
)

METRICS = TelemetryTable(
    "metrics",
    pyarrow.schema(
        [
            *ENVELOPE_FIELDS,
            pyarrow.field("timestamp", pyarrow.timestamp("us"), nullable=False),
            pyarrow.field("name", pyarrow.string(), nullable=False),
            pyarrow.field("type", pyarrow.string(), nullable=False),
            pyarrow.field("value", pyarrow.float64(), nullable=False),
            pyarrow.field("count", pyarrow.int64()),
            pyarrow.field("sum", pyarrow.float64()),
            pyarrow.field("labels", pyarrow.string()),
        ]
    ),
)

TABLES = (EVENTS, METRICS)

# Copies of Suite Cloud's own records beside the telemetry, so a dashboard joins on the cluster
# or the tenant inside one DuckDB instead of across data sources.
NODES = TelemetryTable(
    "nodes",
    pyarrow.schema(
        [
            pyarrow.field("host_ip", pyarrow.string(), nullable=False),
            pyarrow.field("hostname", pyarrow.string(), nullable=False),
            pyarrow.field("cluster", pyarrow.string(), nullable=False),
            pyarrow.field("role", pyarrow.string()),
            pyarrow.field("location", pyarrow.string()),
            pyarrow.field("status", pyarrow.string()),
        ]
    ),
)

MAIL_DOMAINS = TelemetryTable(
    "mail_domains",
    pyarrow.schema(
        [
            pyarrow.field("domain", pyarrow.string(), nullable=False),
            pyarrow.field("tenant", pyarrow.string(), nullable=False),
            pyarrow.field("tenant_title", pyarrow.string()),
        ]
    ),
)

DIMENSIONS = (NODES, MAIL_DOMAINS)
