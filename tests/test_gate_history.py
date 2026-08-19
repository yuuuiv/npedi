from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Config
from client import AuthExpired
from gate import (
    _run_gate_history_worker,
    _stable_history_sample,
    _stable_stratified_history_sample,
    fetch_gate_unit,
)
from store import Store, gate_history_invalid_reason
from sync import build_parser


class FakeGateClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.calls: list[tuple[int, str, str, str]] = []

    def scodeco_page(
        self, page: int, *, direction: str, vessel_code: str, voyage: str
    ) -> dict:
        self.calls.append((page, direction, vessel_code, voyage))
        if page == 2:
            return {"total": 3, "list": [{"id": "evt-3"}]}
        raise AssertionError(f"unexpected page request: {page}")


class GateHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "gate-history.sqlite"
        self.store = Store(self.db)
        self.store.conn.execute(
            """
            CREATE TABLE fact_vessel_plan_snapshot (
                vessel_code TEXT,
                voyage TEXT,
                vessel_en_name TEXT,
                eta TEXT
            )
            """
        )
        self.store.conn.executemany(
            """
            INSERT INTO gate_voyages(
                vesselcode,voyage,vesselename,first_seen_at,last_seen_at,
                gatein_done_at,gatein_total,gateout_done_at,gateout_total
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                ("UN1", "CURRENT", "SHIP ONE", "t", "t", "t", 1, "t", 1),
                ("UN1", "CATALOG", "SHIP ONE", "t", "t", "t", 1, "t", 1),
            ],
        )
        self.store.conn.executemany(
            "INSERT INTO fact_vessel_plan_snapshot VALUES(?,?,?,?)",
            [
                ("UN1", "CURRENT", "SHIP ONE", "2026-05-01T00:00:00+00:00"),
                (" UN1 ", " OLD1 ", "SHIP ONE", "2026-05-03T00:00:00+00:00"),
                ("UN1", "OLD1", "SHIP ONE", "2026-05-04T00:00:00+00:00"),
                ("UN2", "OLD2", "SHIP TWO", "2026-05-05T00:00:00+00:00"),
            ],
        )
        self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_seed_defaults_to_known_vessels_and_is_idempotent(self):
        first = self.store.seed_gate_history_candidates(
            "2026-05-01", "2026-06-01"
        )
        self.assertEqual(first["inserted"], 1)
        row = self.store.conn.execute(
            "SELECT * FROM gate_history_candidate"
        ).fetchone()
        self.assertEqual((row["vesselcode"], row["voyage"]), ("UN1", "OLD1"))
        self.assertEqual(row["source_plan_rows"], 2)

        again = self.store.seed_gate_history_candidates(
            "2026-05-01", "2026-06-01"
        )
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(again["reused"], 1)
        expanded = self.store.seed_gate_history_candidates(
            "2026-05-01", "2026-06-01", known_vessels_only=False
        )
        self.assertEqual(expanded["inserted"], 1)
        keys = {
            tuple(row) for row in self.store.conn.execute(
                "SELECT vesselcode,voyage FROM gate_history_candidate"
            )
        }
        self.assertEqual(keys, {("UN1", "OLD1"), ("UN2", "OLD2")})

    def test_seed_quarantines_unsafe_keys_without_marking_them_empty(self):
        self.store.conn.executemany(
            "INSERT INTO fact_vessel_plan_snapshot VALUES(?,?,?,?)",
            [
                ("0", "2501E", "PLACEHOLDER", "2026-05-06"),
                ("FC0000000", "2502E", "PLACEHOLDER", "2026-05-07"),
                ("UN3", "/", "BAD VOYAGE", "2026-05-08"),
                ("XX1234567", "2503E", "VALID OTHER", "2026-05-09"),
            ],
        )
        self.store.conn.commit()
        result = self.store.seed_gate_history_candidates(
            "2026-05-01", "2026-06-01", known_vessels_only=False,
            refresh=True,
        )
        self.assertEqual(result["rejected"], 3)
        rejected = {
            (row["vesselcode"], row["voyage"], row["reason"])
            for row in self.store.conn.execute(
                "SELECT * FROM gate_history_rejected_pair"
            )
        }
        self.assertIn(("0", "2501E", "placeholder_vesselcode"), rejected)
        self.assertIn(("UN3", "/", "placeholder_voyage"), rejected)
        self.assertIsNotNone(self.store.conn.execute(
            "SELECT 1 FROM gate_history_candidate "
            "WHERE vesselcode='XX1234567' AND voyage='2503E'"
        ).fetchone())
        self.assertIsNone(self.store.conn.execute(
            "SELECT 1 FROM gate_history_candidate WHERE vesselcode='0'"
        ).fetchone())

    def test_invalid_history_key_filter_is_narrow(self):
        self.assertEqual(gate_history_invalid_reason("0", "2501"), "placeholder_vesselcode")
        self.assertEqual(gate_history_invalid_reason("UN1", "/"), "placeholder_voyage")
        self.assertEqual(gate_history_invalid_reason("XX123", "2501E"), "")

    def test_hit_resume_skips_completed_direction(self):
        self.store.seed_gate_history_candidates("2026-05-01", "2026-06-01")
        self.store.note_gate_history_probe(
            "UN1", "OLD1", gatein_total=3, gateout_total=2
        )
        self.store.add_gate_history_voyage("UN1", "OLD1", "SHIP ONE")
        self.store.mark_gate_done("UN1", "OLD1", "GATE_IN", 3)

        row = self.store.gate_history_candidates_pending(
            eta_start="2026-05-01", eta_end_exclusive="2026-06-01", limit=10
        )[0]
        self.assertEqual(row["exact_gate_match"], 1)
        self.assertIsNotNone(row["existing_gatein_done_at"])
        self.assertIsNone(row["existing_gateout_done_at"])

        self.store.mark_gate_done("UN1", "OLD1", "GATE_OUT", 2)
        remaining = self.store.gate_history_candidates_pending(
            eta_start="2026-05-01", eta_end_exclusive="2026-06-01", limit=10
        )
        self.assertEqual(remaining, [])
        self.assertEqual(self.store.gate_history_candidate_counts()["complete"], 1)

    def test_candidate_scope_can_select_unknown_codes_only(self):
        self.store.seed_gate_history_candidates(
            "2026-05-01", "2026-06-01", known_vessels_only=False
        )
        self.assertEqual(
            self.store.gate_history_candidate_count_in_scope(
                "2026-05-01", "2026-06-01",
                statuses=("pending", "hit"),
                vessel_in_catalog=False,
            ),
            1,
        )
        unknown = self.store.gate_history_candidates_pending(
            eta_start="2026-05-01",
            eta_end_exclusive="2026-06-01",
            limit=None,
            vessel_in_catalog=False,
        )
        self.assertEqual(
            [(row["vesselcode"], row["voyage"]) for row in unknown],
            [("UN2", "OLD2")],
        )

    def test_stable_sample_is_reproducible(self):
        candidates = [
            {"vesselcode": f"UN{i}", "voyage": f"V{i}"}
            for i in range(20)
        ]
        first, first_fingerprint = _stable_history_sample(
            candidates, size=7, seed="20260813"
        )
        second, second_fingerprint = _stable_history_sample(
            list(reversed(candidates)), size=7, seed="20260813"
        )
        self.assertEqual(first, second)
        self.assertEqual(first_fingerprint, second_fingerprint)
        self.assertEqual(len(first), 7)

    def test_stable_stratified_sample_honors_prefix_allocations(self):
        candidates = [
            {"vesselcode": f"UN{i}", "voyage": f"U{i}"} for i in range(8)
        ] + [
            {"vesselcode": f"FC{i}", "voyage": f"F{i}"} for i in range(5)
        ] + [
            {"vesselcode": f"CN{i}", "voyage": f"C{i}"} for i in range(2)
        ]
        sample, fingerprint, universe = _stable_stratified_history_sample(
            candidates,
            allocations={"UN": 4, "FC": 3, "CN": 2},
            seed="fixture-v1",
        )
        self.assertEqual(universe, {"CN": 2, "FC": 5, "UN": 8})
        prefixes = [row["vesselcode"][:2] for row in sample]
        self.assertEqual(prefixes.count("UN"), 4)
        self.assertEqual(prefixes.count("FC"), 3)
        self.assertEqual(prefixes.count("CN"), 2)
        self.assertEqual(len(fingerprint), 64)

    def test_first_probe_page_is_reused_when_fetching(self):
        cfg = Config(
            token="fixture",
            db_path=self.db,
            gate_page_size=2,
            request_delay=(0, 0),
            max_pages_per_query=5,
        )
        client = FakeGateClient(cfg)
        run_id = self.store.start_run("fixture", None, None)
        stats, total, _ = fetch_gate_unit(
            client,
            self.store,
            run_id,
            vesselcode="UN1",
            voyage="OLD1",
            direction="GATE_IN",
            label="fixture",
            first_page_data={
                "total": 3,
                "list": [{"id": "evt-1"}, {"id": "evt-2"}],
            },
        )
        self.assertEqual(total, 3)
        self.assertEqual(stats["new"], 3)
        self.assertEqual(client.calls, [(2, "GATE_IN", "UN1", "OLD1")])

    def test_cli_is_bounded_by_default_and_accepts_six_workers(self):
        args = build_parser().parse_args(
            [
                "gate-history-backfill",
                "--eta-start", "2026-05-01",
                "--eta-end", "2026-05-31",
            ]
        )
        self.assertEqual(args.limit, 10)
        self.assertEqual(args.max_requests, 50)
        self.assertEqual(args.delay_ms, 1000)
        self.assertEqual(args.workers, 1)
        self.assertFalse(args.allow_recent)
        self.assertFalse(args.seed_only)

        parallel = build_parser().parse_args(
            [
                "gate-history-backfill",
                "--eta-start", "2026-05-01",
                "--eta-end", "2026-05-31",
                "--workers", "6",
            ]
        )
        self.assertEqual(parallel.workers, 6)

        unknown_sample = build_parser().parse_args(
            [
                "gate-history-backfill",
                "--eta-start", "2026-05-01",
                "--eta-end", "2026-05-31",
                "--unknown-vessels-only",
                "--sample-seed", "20260813",
            ]
        )
        self.assertTrue(unknown_sample.unknown_vessels_only)
        self.assertEqual(unknown_sample.sample_seed, "20260813")

    def test_history_worker_never_performs_automatic_login(self):
        seen: list[Config] = []

        class CapturingClient:
            def __init__(self, cfg: Config):
                seen.append(cfg)
                self.request_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

        cfg = Config(token="fixture", db_path=self.db, auto_login=True)
        with patch("gate.NpediClient", CapturingClient):
            result = _run_gate_history_worker(
                cfg,
                worker_id=1,
                candidates=[],
                run_id=1,
                request_budget=10,
                stop_event=threading.Event(),
            )

        self.assertIsNone(result["error"])
        self.assertTrue(cfg.auto_login)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].auto_login)

    def test_history_worker_surfaces_auth_expiry_with_checkpoint_semantics(self):
        self.store.seed_gate_history_candidates("2026-05-01", "2026-06-01")
        candidate = dict(self.store.gate_history_candidates_pending(
            eta_start="2026-05-01",
            eta_end_exclusive="2026-06-01",
            limit=1,
        )[0])

        class ExpiredClient:
            def __init__(self, cfg: Config):
                self.cfg = cfg
                self.request_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def scodeco_page(self, *_args, **_kwargs):
                self.request_count += 1
                raise AuthExpired("fixture expired")

        cfg = Config(token="fixture", db_path=self.db, auto_login=True)
        with patch("gate.NpediClient", ExpiredClient):
            result = _run_gate_history_worker(
                cfg,
                worker_id=1,
                candidates=[candidate],
                run_id=1,
                request_budget=10,
                stop_event=threading.Event(),
            )
        self.assertIsInstance(result["error"], AuthExpired)
        row = self.store.conn.execute(
            "SELECT status FROM gate_history_candidate "
            "WHERE vesselcode='UN1' AND voyage='OLD1'"
        ).fetchone()
        self.assertEqual(row[0], "pending")

    def test_normal_gate_reads_exclude_quarantined_pairs_without_deleting_raw_rows(self):
        self.store.conn.execute(
            """
            INSERT INTO gate_voyages(
                vesselcode,voyage,vesselename,first_seen_at,last_seen_at,
                gatein_done_at,gatein_total,gateout_done_at,gateout_total
            ) VALUES('UNQ','VQ','VIRTUAL','t','t','t',1,'t',0)
            """
        )
        self.store.conn.execute(
            """
            INSERT INTO gate_events(id,type,vesselcode,voyage,raw_json,fetched_at)
            VALUES('quarantined-event','GATE_IN','UNQ','VQ','{}','t')
            """
        )
        self.store.conn.execute(
            """
            INSERT INTO gate_history_rejected_pair(
                vesselcode,voyage,reason,vesselename,first_eta,last_eta,
                source_plan_rows,discovered_at,updated_at
            ) VALUES('UNQ','VQ','sidecar:fixture','VIRTUAL','2026-05-01',
                     '2026-05-01',1,'t','t')
            """
        )
        self.store.conn.commit()

        counts = self.store.gate_counts()
        self.assertEqual(counts["events_quarantined"], 1)
        self.assertNotIn(
            "quarantined-event",
            {row["id"] for row in self.store.iter_gate_events()},
        )
        self.assertIn(
            "quarantined-event",
            {
                row["id"]
                for row in self.store.iter_gate_events(include_quarantined=True)
            },
        )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM gate_events WHERE id='quarantined-event'"
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
