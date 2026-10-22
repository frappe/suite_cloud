import json
import unittest
from pathlib import Path

from suite_cloud.telemetry import EVENT_DATA_COLUMNS

WORKBOOKS = Path(__file__).resolve().parents[1] / "insights_workbook"


def shipped_workbook(name: str) -> dict:
    return json.loads((WORKBOOKS / name / f"{name}.json").read_text())


class TestShippedWorkbooks(unittest.TestCase):
    def test_metric_charts_are_plotted_per_hour(self) -> None:
        """A gauge sampled every minute shows one point per day when bucketed by day; the host
        and Stalwart metric lines bucket by hour, with room for a week of points per host."""

        for workbook in ("stalwart", "machines"):
            for name, chart in shipped_workbook(workbook)["charts"].items():
                x_axis = chart["config"].get("x_axis", {}).get("dimension", {})
                if x_axis.get("column_name") != "timestamp" or "metrics" not in chart["query"]:
                    continue
                with self.subTest(chart=name):
                    self.assertEqual(x_axis.get("granularity"), "hour")
                    self.assertGreaterEqual(chart["config"].get("limit", 0), 1000)
                    self.assertNotIn("per day", chart.get("info") or "")

    def test_every_dashboard_opens_on_a_bounded_date_range(self) -> None:
        """Without a default every chart reads the whole retention on the first load."""

        for workbook in ("stalwart", "machines"):
            for name, dashboard in shipped_workbook(workbook)["dashboards"].items():
                for item in dashboard["items"]:
                    if item["type"] == "filter" and item["filter_type"] == "Date":
                        with self.subTest(dashboard=name):
                            self.assertEqual(item["default_operator"], "within")
                            self.assertTrue(item.get("default_value"))

    def test_the_events_query_reads_the_extracted_columns_instead_of_parsing_data(self) -> None:
        query = shipped_workbook("stalwart")["queries"]["stalwart-events"]
        mutates = [operation for operation in query["operations"] if operation["type"] == "mutate"]
        selected = next(operation for operation in query["operations"] if operation["type"] == "select")

        for mutate in mutates:
            self.assertNotIn("data", mutate["expression"]["expression"])
        self.assertLessEqual({column.name for column in EVENT_DATA_COLUMNS}, set(selected["column_names"]))
