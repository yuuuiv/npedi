from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config import Config
from crawlers import CargoReleaseCrawler, ContainerHistoryCrawler, TransshipmentCrawler, VgmCrawler
from normalize import normalize_cargo_release
from npedi import _container_history_batch
from timeseries import TimeseriesStore

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "core_facts.json").read_text(encoding="utf-8-sig"))


class FactClient:
    def __init__(self):
        self.vgm_filters = []
        self.release_filters = []

    def vgm_page(self, page, *, page_size, **filters):
        self.vgm_filters.append(filters)
        return FIXTURE["vgm"]

    def cargo_release_page(self, page, *, page_size, **filters):
        self.release_filters.append(filters)
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
            store.conn.execute("""INSERT INTO silver_container_notice
                (business_key_hash, vessel_code, voyage, ingested_at, record_hash, raw_json)
                VALUES ('notice-key', 'UN0000009', 'V1', '2026-08-05T00:00:00Z', 'notice-hash', '{}')""")
            store.conn.commit()
            self.assertEqual(CargoReleaseCrawler(self.client, store, self.cfg).crawl()["inserted"], 1)
            self.assertEqual(self.client.release_filters[0], {"vesselcode": "UN0000009", "voyage": "V1"})
            self.assertEqual(TransshipmentCrawler(self.client, store, self.cfg).crawl()["inserted"], 1)
            second = VgmCrawler(self.client, store, self.cfg).crawl({"container_nos": ["ABCU1234567"]})
            self.assertEqual(second["inserted"], 0)
            self.assertIsNone(store.conn.execute("SELECT gross_weight FROM fact_cargo_release").fetchone()[0])
            cargo = store.conn.execute(
                "SELECT piece_count,gross_weight_kg,gross_weight_unit FROM fact_cargo_release"
            ).fetchone()
            self.assertEqual(cargo[0], 12.5)
            self.assertIsNone(cargo[1])
            self.assertIsNone(cargo[2])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM container_enrichment_queue").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT cargo_group_name FROM fact_transshipment").fetchone()[0], "UNKNOWN")

    def test_cargo_release_stops_when_endpoint_ignores_filters(self):
        with TimeseriesStore(self.db) as store:
            store.conn.execute("""INSERT INTO silver_container_notice
                (business_key_hash, vessel_code, voyage, ingested_at, record_hash, raw_json)
                VALUES ('notice-key', 'VESSEL1', 'VOYAGE1', '2026-08-05T00:00:00Z', 'notice-hash', '{}')""")
            store.conn.commit()

            result = CargoReleaseCrawler(self.client, store, self.cfg).crawl()

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["requests"], 1)
            self.assertEqual(result["seen"], 0)
            self.assertEqual(self.client.release_filters, [{"vesselcode": "VESSEL1", "voyage": "VOYAGE1"}])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM fact_cargo_release").fetchone()[0], 0)
            error = store.conn.execute("SELECT message FROM ingest_error WHERE stage='response_validation'").fetchone()[0]
            self.assertIn("ignored filters", error)

    def test_history_keeps_unknown_action_code(self):
        with TimeseriesStore(self.db) as store:
            store.conn.execute("""INSERT INTO container_enrichment_queue
                (container_no, priority, source, first_seen_at, status)
                VALUES ('ABCU1234567', 0, 'fixture', '2026-08-05T00:00:00Z', 'pending')""")
            store.conn.commit()
            result = ContainerHistoryCrawler(self.client, store, self.cfg).crawl({"container_nos": ["ABCU1234567"]})
            self.assertEqual(result["inserted"], 1)
            row = store.conn.execute("SELECT event_type,event_code_raw,event_time FROM fact_container_event").fetchone()
            self.assertEqual(tuple(row), ("UNKNOWN", "ZZ", None))
            queue = store.conn.execute("SELECT status,attempt_count FROM container_enrichment_queue WHERE container_no='ABCU1234567'").fetchone()
            self.assertEqual(tuple(queue), ("complete", 1))

    def test_history_batch_honors_limit_and_offset(self):
        with TimeseriesStore(self.db) as store:
            for number in ("ABCU0000017", "ABCU0000022", "ABCU0000038"):
                store.conn.execute("""INSERT INTO container_enrichment_queue
                    (container_no, priority, source, first_seen_at, status)
                    VALUES (?, 0, 'fixture', ?, 'pending')""", (number, number,))
            store.conn.commit()

            self.assertEqual(_container_history_batch(store, 2, 0), ["ABCU0000017", "ABCU0000022"])
            self.assertEqual(_container_history_batch(store, 2, 2), ["ABCU0000038"])

    def test_cargo_release_weight_rule_is_explicit_and_auditable(self):
        row = normalize_cargo_release({"grossweight": "318014000", "cargovolum": "9"})
        self.assertEqual(row["gross_weight"], 318014000.0)
        self.assertEqual(row["gross_weight_kg"], 318014000.0)
        self.assertEqual(row["gross_weight_unit"], "kg")
        self.assertEqual(row["weight_rule_version"], "cargo-weight-kg-v1")
        self.assertEqual(row["piece_count"], 9.0)


if __name__ == "__main__":
    unittest.main()
