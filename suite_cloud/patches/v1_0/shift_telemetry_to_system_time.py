import frappe
from frappe.utils import get_system_timezone

from suite_cloud.telemetry import TABLES
from suite_cloud.telemetry.warehouse import Warehouse

TIME_COLUMNS = ("received_at", "timestamp")


def execute(warehouse: Warehouse | None = None) -> None:
    """Telemetry times are stored in the system timezone now, so the charts read in it. The rows
    already stored are naive UTC: the staged ones are loaded first, then every row is moved.

    One transaction, and no move while a batch is still staged: a second run would move the rows
    again, and a UTC batch loaded later would stay behind.
    """

    warehouse = warehouse or Warehouse()
    if not frappe.db.exists("Insights Data Source v3", warehouse.data_source):
        return
    warehouse.load_pending()
    if any(warehouse.spool.get_pending(table, 1) for table in TABLES):
        frappe.throw("Staged telemetry batches did not load; see the Error Log and migrate again.")
    assignments = ", ".join(
        f'"{column}" = timezone($zone, timezone(\'UTC\', "{column}"))' for column in TIME_COLUMNS
    )
    with warehouse.write_connection() as db:
        db.con.begin()
        for table in TABLES:
            db.con.execute(f'UPDATE "{table.name}" SET {assignments}', {"zone": get_system_timezone()})
        db.con.commit()
        db.raw_sql("CHECKPOINT")
