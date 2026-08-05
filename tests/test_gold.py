from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aggregate import rebuild_gold
from config import Config
from crawlers import CargoReleaseCrawler, TransshipmentCrawler, VgmCrawler
from curves import AGGREGATIONS, build_curves
from quality import quality_report, write_quality_report
from test_phase1 import FakeClient as PlanClient
from timeseries import TimeseriesStore, VesselPlanCrawler
from test_facts import FactClient


class GoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "gold.sqlite"
        self.cfg = Config(token="fixture", db_path=self.db, page_size=2, request_delay=(0, 0), max_pages_per_query=5)

    def tearDown(self):
        self.tmp.cleanup()

    def test_aggregate_curves_quality_and_reports(self):
        with TimeseriesStore(self.db) as store:
            VesselPlanCrawler(PlanClient(), store, self.cfg).crawl()
            facts = FactClient()
            VgmCrawler(facts, store, self.cfg).crawl({"container_nos": ["ABCU1234567"]})
            CargoReleaseCrawler(facts, store, self.cfg).crawl()
            TransshipmentCrawler(facts, store, self.cfg).crawl()
            counts = rebuild_gold(store)
            self.assertGreaterEqual(counts["daily"], 1)
            self.assertGreaterEqual(counts["weekly"], 1)
            curve_count = build_curves(store, granularity="day")
            self.assertGreater(curve_count, 0)
            day_count = store.conn.execute("SELECT COUNT(*) FROM mart_curve_series WHERE granularity='day'").fetchone()[0]
            self.assertGreater(build_curves(store, granularity="week"), 0)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM mart_curve_series WHERE granularity='day'").fetchone()[0],
                day_count,
            )
            curve_types = {row[0] for row in store.conn.execute("SELECT DISTINCT curve_type FROM mart_curve_series")}
            self.assertIn("vgm", curve_types)
            self.assertEqual(AGGREGATIONS["plan_revision"], "sum")
            self.assertIn("pressure_index", curve_types)
            report = quality_report(store)
            self.assertEqual(report["pagination"]["repeated_pages"], 0)
            output = Path(self.tmp.name) / "quality.md"
            write_quality_report(store, output)
            self.assertTrue(output.exists())
            self.assertTrue(output.with_suffix(".json").exists())


if __name__ == "__main__":
    unittest.main()
