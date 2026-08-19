from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from gate_anomaly_sidecar import (
    SidecarStore,
    capture_jobs,
    discover,
    ensure_separate_database,
    main_history_status,
    quarantine_main,
)


class FakeClient:
    def __init__(self, pages: dict[int, dict]):
        self.pages = pages
        self.request_count = 0
        self.calls: list[tuple[int, str, str, str, int]] = []

    def scodeco_page(
        self,
        page: int,
        *,
        direction: str,
        vessel_code: str,
        voyage: str,
        page_size: int,
    ) -> dict:
        self.request_count += 1
        self.calls.append((page, direction, vessel_code, voyage, page_size))
        return self.pages[page]


class GateAnomalySidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.main_db = self.root / "main.sqlite"
        self.sidecar_db = self.root / "sidecar.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def create_main_fixture(self) -> None:
        conn = sqlite3.connect(self.main_db)
        conn.executescript(
            """
            CREATE TABLE gate_history_candidate (
                vesselcode TEXT NOT NULL,
                voyage TEXT NOT NULL,
                vesselename TEXT,
                first_eta TEXT NOT NULL,
                last_eta TEXT NOT NULL,
                source_plan_rows INTEGER NOT NULL DEFAULT 0,
                vessel_in_catalog INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                probed_at TEXT,
                gatein_total INTEGER,
                gateout_total INTEGER,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(vesselcode,voyage)
            );
            CREATE TABLE gate_history_candidate_month (
                vesselcode TEXT NOT NULL,
                voyage TEXT NOT NULL,
                eta_month TEXT NOT NULL,
                vesselename TEXT,
                first_eta TEXT NOT NULL,
                last_eta TEXT NOT NULL,
                source_plan_rows INTEGER NOT NULL,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(vesselcode,voyage,eta_month)
            );
            CREATE TABLE gate_history_rejected_pair (
                vesselcode TEXT NOT NULL,
                voyage TEXT NOT NULL,
                reason TEXT NOT NULL,
                vesselename TEXT,
                first_eta TEXT NOT NULL,
                last_eta TEXT NOT NULL,
                source_plan_rows INTEGER NOT NULL DEFAULT 0,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(vesselcode,voyage)
            );
            CREATE TABLE fact_vessel_plan_snapshot (
                vessel_plan_key INTEGER PRIMARY KEY AUTOINCREMENT,
                vessel_code TEXT,
                vessel_en_name TEXT,
                vessel_cn_name TEXT,
                voyage TEXT,
                eta TEXT,
                snapshot_time TEXT,
                raw_json TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO gate_history_candidate(
                vesselcode,voyage,vesselename,first_eta,last_eta,source_plan_rows,
                status,gatein_total,gateout_total,discovered_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    "UN7654321", "11", "ZHONGZHUANMEISHAN",
                    "2026-03-05", "2026-03-05", 1, "hit", 1_633_402, 0,
                    "2026-08-14", "2026-08-14",
                ),
                (
                    "UNREAL1", "R1", "REAL SHIP", "2026-03-06", "2026-03-06",
                    1, "complete", 80_000, 0, "2026-08-14", "2026-08-14",
                ),
                (
                    "UNVIRTUAL", "V1", "ZHONGZHUANOTHER",
                    "2026-04-01", "2026-04-01", 1, "pending", None, None,
                    "2026-08-14", "2026-08-14",
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO gate_history_candidate_month VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                ("UN7654321", "11", "2026-03", "ZHONGZHUANMEISHAN", "2026-03-05", "2026-03-05", 1, "t", "t"),
                ("UNREAL1", "R1", "2026-03", "REAL SHIP", "2026-03-06", "2026-03-06", 1, "t", "t"),
                ("UNVIRTUAL", "V1", "2026-04", "ZHONGZHUANOTHER", "2026-04-01", "2026-04-01", 1, "t", "t"),
            ],
        )
        for code, voyage, en_name, cn_name, eta, raw in [
            (
                "UN7654321", "11", "ZHONGZHUANMEISHAN", "中转梅山", "2026-03-05",
                {"vesselCode": "ZZZMS", "vesselSysid": "0", "imo": None, "mmsi": None},
            ),
            (
                "UNVIRTUAL", "V1", "ZHONGZHUANOTHER", "中转测试", "2026-04-01",
                {"vesselCode": "ZZZX", "vesselSysid": 0, "imo": "", "mmsi": ""},
            ),
        ]:
            conn.execute(
                """
                INSERT INTO fact_vessel_plan_snapshot(
                    vessel_code,vessel_en_name,vessel_cn_name,voyage,eta,snapshot_time,raw_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (code, en_name, cn_name, voyage, eta, "2026-08-14", json.dumps(raw)),
            )
        conn.commit()
        conn.close()

    def seed_one(self, *, total: int = 3, page_size: int = 2, page_cap: int = 20_000) -> SidecarStore:
        store = SidecarStore(self.sidecar_db, main_db_path=self.main_db)
        store.seed_candidate(
            vesselcode="UN7654321",
            voyage="11",
            vessel_name="ZHONGZHUANMEISHAN",
            first_eta="2026-03-05",
            last_eta="2026-03-05",
            source_plan_rows=1,
            reason="virtual_transshipment_entity",
            evidence={"fixture": True},
            source_plan={"vesselCode": "ZZZMS"},
            planned_totals={"GATE_IN": total, "GATE_OUT": 0},
            page_size=page_size,
            page_cap=page_cap,
        )
        # Keep this fixture focused on one direction.
        store.conn.execute("UPDATE capture_job SET status='complete' WHERE query_direction='GATE_OUT'")
        store.conn.commit()
        return store

    def test_sidecar_must_not_be_main_database(self):
        with self.assertRaises(ValueError):
            ensure_separate_database(self.main_db, self.main_db)

    def test_discovery_finds_virtual_and_unimported_oversized_only(self):
        self.create_main_fixture()
        result = discover(
            main_db_path=self.main_db,
            sidecar_path=self.sidecar_db,
            eta_start="2026-01-01",
            eta_end="2026-06-30",
            total_threshold=50_000,
            page_size=100,
            page_cap=20_000,
        )
        self.assertEqual(result["found"], 2)
        with SidecarStore(self.sidecar_db, main_db_path=self.main_db) as store:
            rows = store.conn.execute(
                "SELECT query_vesselcode,isolation_reason FROM capture_candidate ORDER BY 1"
            ).fetchall()
        self.assertEqual([row["query_vesselcode"] for row in rows], ["UN7654321", "UNVIRTUAL"])
        self.assertIn("oversized_unvalidated_response", rows[0]["isolation_reason"])
        self.assertIn("operational_bucket:transship_aggregate", rows[0]["isolation_reason"])

    def test_quarantine_main_is_idempotent_and_keeps_sidecar_payload_separate(self):
        self.create_main_fixture()
        discover(
            main_db_path=self.main_db,
            sidecar_path=self.sidecar_db,
            eta_start="2026-01-01",
            eta_end="2026-06-30",
            total_threshold=50_000,
            page_size=100,
            page_cap=20_000,
        )
        self.assertEqual(quarantine_main(main_db_path=self.main_db, sidecar_path=self.sidecar_db), 2)
        self.assertEqual(quarantine_main(main_db_path=self.main_db, sidecar_path=self.sidecar_db), 0)
        conn = sqlite3.connect(self.main_db)
        rows = conn.execute(
            "SELECT vesselcode,reason FROM gate_history_rejected_pair ORDER BY vesselcode"
        ).fetchall()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(reason.startswith("sidecar:") for _, reason in rows))
        self.assertNotIn("capture_row", tables)

    def test_main_history_status_excludes_exact_quarantined_pairs(self):
        self.create_main_fixture()
        discover(
            main_db_path=self.main_db,
            sidecar_path=self.sidecar_db,
            eta_start="2026-01-01",
            eta_end="2026-06-30",
            total_threshold=50_000,
            page_size=100,
            page_cap=20_000,
        )
        before = main_history_status(
            main_db_path=self.main_db,
            eta_start="2026-01-01",
            eta_end="2026-06-30",
        )
        self.assertEqual(before["remaining"], 2)
        quarantine_main(main_db_path=self.main_db, sidecar_path=self.sidecar_db)
        after = main_history_status(
            main_db_path=self.main_db,
            eta_start="2026-01-01",
            eta_end="2026-06-30",
        )
        self.assertEqual(after["remaining"], 0)

    def test_capture_resumes_at_page_boundary_and_never_injects_query_identity(self):
        store = self.seed_one()
        try:
            run1 = store.begin_run(request_budget=1, delay_ms=0, token="secret")
            first = FakeClient({
                1: {
                    "total": 3,
                    "list": [
                        {"id": "a", "voyage": "REMOTE", "vessel": "A"},
                        {"id": "b", "voyage": "REMOTE", "vessel": "B"},
                    ],
                }
            })
            stats1 = capture_jobs(
                store=store, client=first, run_id=run1, request_budget=1
            )
            self.assertEqual(stats1["pages"], 1)
            job = store.conn.execute(
                "SELECT * FROM capture_job WHERE query_direction='GATE_IN'"
            ).fetchone()
            self.assertEqual((job["status"], job["next_page"]), ("partial", 2))

            run2 = store.begin_run(request_budget=1, delay_ms=0, token="secret")
            second = FakeClient({
                2: {"total": 3, "list": [{"id": "c", "voyage": "REMOTE", "vessel": "C"}]}
            })
            stats2 = capture_jobs(
                store=store, client=second, run_id=run2, request_budget=1
            )
            self.assertEqual(stats2["completed_jobs"], 1)
            job = store.conn.execute(
                "SELECT * FROM capture_job WHERE query_direction='GATE_IN'"
            ).fetchone()
            self.assertEqual(job["status"], "complete")
            identities = store.conn.execute(
                "SELECT DISTINCT response_vesselcode,response_voyage FROM capture_row"
            ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in identities], [(None, "REMOTE")])
            candidate = store.conn.execute(
                "SELECT query_vesselcode,query_voyage FROM capture_candidate"
            ).fetchone()
            self.assertEqual(tuple(candidate), ("UN7654321", "11"))
        finally:
            store.close()

    def test_shrinking_total_pauses_job_without_crossing_page(self):
        store = self.seed_one(total=6, page_size=2)
        try:
            run1 = store.begin_run(request_budget=1, delay_ms=0, token="x")
            capture_jobs(
                store=store,
                client=FakeClient({1: {"total": 6, "list": [{"id": "a"}, {"id": "b"}]}}),
                run_id=run1,
                request_budget=1,
            )
            run2 = store.begin_run(request_budget=1, delay_ms=0, token="x")
            stats = capture_jobs(
                store=store,
                client=FakeClient({2: {"total": 4, "list": [{"id": "c"}, {"id": "d"}]}}),
                run_id=run2,
                request_budget=1,
            )
            self.assertEqual(stats["errors"], 1)
            job = store.conn.execute(
                "SELECT status,next_page,last_error FROM capture_job"
                " WHERE query_direction='GATE_IN'"
            ).fetchone()
            self.assertEqual((job[0], job[1]), ("error", 2))
            self.assertIn("缩水", job[2])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM capture_page").fetchone()[0], 1)
        finally:
            store.close()

    def test_growing_total_keeps_page_plan_frozen_on_first_total(self):
        # An accumulating bucket adds rows mid-walk: the job must keep going and
        # finish the page-1 snapshot instead of chasing the moving tail.
        store = self.seed_one(total=4, page_size=2)
        try:
            run1 = store.begin_run(request_budget=1, delay_ms=0, token="x")
            capture_jobs(
                store=store,
                client=FakeClient({1: {"total": 4, "list": [{"id": "a"}, {"id": "b"}]}}),
                run_id=run1,
                request_budget=1,
            )
            run2 = store.begin_run(request_budget=4, delay_ms=0, token="x")
            client = FakeClient({2: {"total": 9, "list": [{"id": "c"}, {"id": "d"}]}})
            stats = capture_jobs(
                store=store, client=client, run_id=run2, request_budget=4
            )
            self.assertEqual(stats["errors"], 0)
            self.assertEqual(stats["completed_jobs"], 1)
            # Page 3 exists under the grown total but is outside the frozen plan.
            self.assertEqual([call[0] for call in client.calls], [2])
            job = store.conn.execute(
                "SELECT status,next_page,first_total,last_total,expected_pages,rows_saved"
                " FROM capture_job WHERE query_direction='GATE_IN'"
            ).fetchone()
            self.assertEqual(tuple(job), ("complete", 3, 4, 9, 2, 4))
        finally:
            store.close()

    def test_growth_still_rejects_a_short_page_inside_the_frozen_window(self):
        store = self.seed_one(total=6, page_size=2)
        try:
            run1 = store.begin_run(request_budget=1, delay_ms=0, token="x")
            capture_jobs(
                store=store,
                client=FakeClient({1: {"total": 6, "list": [{"id": "a"}, {"id": "b"}]}}),
                run_id=run1,
                request_budget=1,
            )
            run2 = store.begin_run(request_budget=1, delay_ms=0, token="x")
            stats = capture_jobs(
                store=store,
                client=FakeClient({2: {"total": 8, "list": [{"id": "c"}]}}),
                run_id=run2,
                request_budget=1,
            )
            self.assertEqual(stats["errors"], 1)
            job = store.conn.execute(
                "SELECT status,next_page,last_error FROM capture_job"
                " WHERE query_direction='GATE_IN'"
            ).fetchone()
            self.assertEqual((job[0], job[1]), ("error", 2))
            self.assertIn("拒绝留下分页缺口", job[2])
        finally:
            store.close()

    def test_sidecar_can_resume_beyond_main_pipeline_five_thousand_page_cap(self):
        store = self.seed_one(total=6_000, page_size=1, page_cap=20_000)
        try:
            store.conn.execute(
                """
                UPDATE capture_job SET next_page=5001,first_total=6000,last_total=6000,
                    expected_pages=6000,status='partial'
                WHERE query_direction='GATE_IN'
                """
            )
            store.conn.commit()
            run_id = store.begin_run(request_budget=1, delay_ms=0, token="x")
            client = FakeClient({5001: {"total": 6000, "list": [{"id": "p5001"}]}})
            stats = capture_jobs(
                store=store, client=client, run_id=run_id, request_budget=1
            )
            self.assertEqual(stats["pages"], 1)
            self.assertEqual(client.calls[0][0], 5001)
            next_page = store.conn.execute(
                "SELECT next_page FROM capture_job WHERE query_direction='GATE_IN'"
            ).fetchone()[0]
            self.assertEqual(next_page, 5002)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
