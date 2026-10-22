import json
import unittest
from pathlib import Path

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
