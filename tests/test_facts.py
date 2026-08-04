from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config import Config
from crawlers import CargoReleaseCrawler, ContainerHistoryCrawler, TransshipmentCrawler, VgmCrawler
from npedi import _container_history_batch
from timeseries import TimeseriesStore

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "core_facts.json").read_text(encoding="utf-8-sig"))


class FactClient:
    def __init__(self):
        self.vgm_filters = []

    def vgm_page(self, page, *, page_size, **filters):
        self.vgm_filters.append(filters)
        return FIXTURE["vgm"]

    def cargo_release_page(self, page, *, page_size, **filters):
        return FIXTURE["release"]

    def transshipment_page(self, page, *, page_size, **filters):
        return FIXTURE["transshipment"]

    def container_history(self, container_no):
        return FIXTURE["history"]


class FactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "facts.sqlite"
        self.cfg = Config(token="fixture", db_path=self.db, page_size=2, request_delay=(0, 0), max_pages_per_query=5)
        self.client = FactClient()

    def tearDown(self):
        self.tmp.cleanup()

    def test_facts_are_normalized_and_idempotent(self):
        with TimeseriesStore(self.db) as store:
            self.assertEqual(VgmCrawler(self.client, store, self.cfg).crawl({"container_nos": ["ABCU1234567"]})["inserted"], 1)
            self.assertEqual(self.client.vgm_filters[0], {"containerNumber": "ABCU1234567"})
            self.assertEqual(CargoReleaseCrawler(self.client, store, self.cfg).crawl()["inserted"], 1)
            self.assertEqual(TransshipmentCrawler(self.client, store, self.cfg).crawl()["inserted"], 1)
            second = VgmCrawler(self.client, store, self.cfg).crawl({"container_nos": ["ABCU1234567"]})
            self.assertEqual(second["inserted"], 0)
            self.assertIsNone(store.conn.execute("SELECT gross_weight FROM fact_cargo_release").fetchone()[0])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM container_enrichment_queue").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT cargo_group_name FROM fact_transshipment").fetchone()[0], "UNKNOWN")

    def test_history_keeps_unknown_action_code(self):
        with TimeseriesStore(self.db) as store:
            result = ContainerHistoryCrawler(self.client, store, self.cfg).crawl({"container_nos": ["ABCU1234567"]})
            self.assertEqual(result["inserted"], 1)
            row = store.conn.execute("SELECT event_type,event_code_raw,event_time FROM fact_container_event").fetchone()
            self.assertEqual(tuple(row), ("UNKNOWN", "ZZ", None))

    def test_history_batch_honors_limit_and_offset(self):
        with TimeseriesStore(self.db) as store:
            for number in ("ABCU0000001", "ABCU0000002", "ABCU0000003"):
                store.conn.execute("""INSERT INTO container_enrichment_queue
                    (container_no, priority, source, first_seen_at, status)
                    VALUES (?, 0, 'fixture', ?, 'pending')""", (number, number,))
            store.conn.commit()

            self.assertEqual(_container_history_batch(store, 2, 0), ["ABCU0000001", "ABCU0000002"])
            self.assertEqual(_container_history_batch(store, 2, 2), ["ABCU0000003"])


if __name__ == "__main__":
    unittest.main()
