from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from client import ApiError, AuthExpired, NpediClient
from config import Config
from normalize import classify_cargo
from timeseries import BaseCrawler, RequestSpec, TimeseriesStore, parse_number, parse_time


class RepeatClient:
    def vessel_plan_page(self, page, *, page_size, **filters):
        return {"code": 200, "data": {"page": page, "pageSize": page_size, "total": 10, "totalPages": 0, "list": [{"vesselUnCode": "UN1", "voyage": "V1", "terminal": "T", "vesselDirect": "E", "eta": "20260801000000"}]}}


class TinyCrawler(BaseCrawler):
    endpoint_name = "repeat_fixture"

    def fetch_page(self, request: RequestSpec, page: int):
        return self.client.vessel_plan_page(page, page_size=request.page_size)


class RequestErrorClient:
    def vessel_plan_page(self, page, *, page_size, **filters):
        raise ApiError("fixture HTTP 400")


class RequestErrorCrawler(BaseCrawler):
    endpoint_name = "request_error_fixture"

    def fetch_page(self, request: RequestSpec, page: int):
        return self.client.vessel_plan_page(page, page_size=request.page_size)


class ContractTests(unittest.TestCase):
    def test_auth_401_and_403_stop(self):
        for status in (401, 403):
            cfg = Config(token="fixture", request_delay=(0, 0))
            client = NpediClient(cfg)
            client._http.close()
            client._http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status)), base_url=cfg.api_base)
            with self.assertRaises(AuthExpired):
                client.get_json("/fixture")
            client.close()

    def test_repeated_page_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "repeat.sqlite"
            cfg = Config(token="fixture", db_path=db, page_size=1, request_delay=(0, 0), max_pages_per_query=5)
            with TimeseriesStore(db) as store:
                result = TinyCrawler(RepeatClient(), store, cfg).crawl()
                self.assertEqual(result["errors"], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM ingest_error WHERE stage='pagination'").fetchone()[0], 1)

    def test_api_error_is_recorded_as_partial_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "request-error.sqlite"
            cfg = Config(token="fixture", db_path=db, page_size=1, request_delay=(0, 0), max_pages_per_query=5)
            with TimeseriesStore(db) as store:
                result = RequestErrorCrawler(RequestErrorClient(), store, cfg).crawl()
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["errors"], 1)
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM ingest_error WHERE stage='request'").fetchone()[0], 1)

    def test_parser_and_cargo_rule_contracts(self):
        self.assertEqual(parse_number(" 12.5 "), 12.5)
        self.assertIsNone(parse_number("bad"))
        self.assertIsNotNone(parse_time("20260801000000"))
        self.assertIsNone(parse_time("not-a-date"))
        self.assertEqual(classify_cargo("  Steel  ", {"STEEL": "METAL"})[0], "METAL")
        self.assertEqual(classify_cargo("Frozen fish", {}, [("fish", "FOOD")])[0], "FOOD")
        self.assertEqual(classify_cargo("unknown", {})[0], "UNKNOWN")

    def test_empty_database_migration_creates_required_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TimeseriesStore(Path(tmp) / "empty.sqlite") as store:
                tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for name in ("crawl_run", "crawl_checkpoint", "raw_api_response", "ingest_error", "fact_container_vgm", "agg_flow_daily", "mart_curve_series", "cluster_run"):
                    self.assertIn(name, tables)


if __name__ == "__main__":
    unittest.main()
