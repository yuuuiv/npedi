from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config import Config
from timeseries import ContainerNoticeCrawler, TimeseriesStore, VesselPlanCrawler

ROOT = Path(__file__).parent


class FakeClient:
    def __init__(self):
        self.plan_calls = []
        self.notice_calls = []

    def vessel_plan_page(self, page: int, *, page_size: int, **_: str) -> dict:
        self.plan_calls.append(page)
        if page > 2:
            return {"code": 200, "msg": "操作成功", "data": {"page": page, "pageSize": page_size, "total": 3, "list": []}}
        return json.loads((ROOT / "fixtures" / f"vessel_plan_page_{page}.json").read_text(encoding="utf-8-sig"))

    def container_notice_page(self, page: int, *, page_size: int, **_: str) -> dict:
        self.notice_calls.append(page)
        if page > 1:
            return {"code": 200, "msg": "操作成功", "data": {"pageNum": page, "pageSize": page_size, "total": 2, "list": []}}
        return json.loads((ROOT / "fixtures" / "container_notice_page_1.json").read_text(encoding="utf-8-sig"))


class Phase1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite"
        self.cfg = Config(token="fixture", db_path=self.db, page_size=2, request_delay=(0, 0), max_pages_per_query=5)
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_pagination_and_idempotency(self):
        with TimeseriesStore(self.db) as store:
            first = VesselPlanCrawler(self.client, store, self.cfg).crawl()
            self.assertEqual(first["seen"], 3)
            self.assertEqual(first["inserted"], 3)
            self.assertEqual(self.client.plan_calls, [1, 2])
            second = VesselPlanCrawler(self.client, store, self.cfg).crawl()
            self.assertEqual(second["seen"], 3)
            self.assertEqual(second["inserted"], 3)  # each snapshot is retained
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM raw_api_response").fetchone()[0], 2)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM silver_vessel_plan").fetchone()[0], 6)

    def test_notice_normalizes_name_and_keeps_raw(self):
        with TimeseriesStore(self.db) as store:
            result = ContainerNoticeCrawler(self.client, store, self.cfg).crawl()
            self.assertEqual(result["inserted"], 2)
            row = store.conn.execute("SELECT * FROM silver_container_notice").fetchone()
            self.assertEqual(row["vessel_en_name"], "TEST SHIP")
            self.assertEqual(row["terminal_code"], "T1")
            self.assertIn("ediports", row["raw_json"])

    def test_checkpoint_resume_starts_at_saved_page(self):
        with TimeseriesStore(self.db) as store:
            store.save_checkpoint("vessel_plan", "default", 2, 3)
            result = VesselPlanCrawler(self.client, store, self.cfg).crawl(resume=True)
            self.assertEqual(self.client.plan_calls, [2])
            self.assertEqual(result["seen"], 1)


if __name__ == "__main__":
    unittest.main()
