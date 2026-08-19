from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from config import Config
from store import Store
from sync import observe_token_auth_failure, observe_token_run_ok, preflight


class FakeInfoClient:
    def __init__(self) -> None:
        self.request_count = 0

    def get_info(self) -> dict:
        self.request_count += 1
        return {"code": 200}


class RefreshingInfoClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.request_count = 0

    def get_info(self) -> dict:
        self.request_count += 1
        self.cfg.token = "renewed-fixture-token"
        return {"code": 200}


class TokenObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "token.sqlite"
        self.store = Store(self.db)
        self.cfg = Config(
            token="fixture-secret-token",
            db_path=self.db,
            alert_file=Path(self.tmp.name) / "alert",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_preflight_and_run_success_keep_first_and_advance_last(self) -> None:
        run_id = self.store.start_run("fixture", None, None)
        fp = preflight(
            self.cfg, FakeInfoClient(), self.store,
            datetime(2026, 8, 13, 10, 0, 0),
            run_id=run_id, run_kind="fixture",
        )
        first = self.store.conn.execute(
            "SELECT * FROM auth_token_observation WHERE fingerprint=?", (fp,)
        ).fetchone()
        self.assertEqual(first["first_success_at"], "2026-08-13 10:00:00")
        self.assertEqual(first["successful_preflights"], 1)

        observe_token_run_ok(
            self.store, self.cfg, run_id=run_id, run_kind="fixture",
            request_count=17,
        )
        second = self.store.conn.execute(
            "SELECT * FROM auth_token_observation WHERE fingerprint=?", (fp,)
        ).fetchone()
        self.assertEqual(second["first_success_at"], "2026-08-13 10:00:00")
        self.assertEqual(second["successful_runs"], 1)
        self.assertEqual(second["successful_requests"], 17)
        self.assertNotIn("fixture-secret-token", str(tuple(second)))

    def test_seen_observation_keeps_the_earliest_timestamp(self) -> None:
        self.store.observe_token_seen("a" * 64, "2026-08-13 12:00:00")
        self.store.observe_token_seen("a" * 64, "2026-08-13 10:00:00")
        self.assertEqual(
            self.store.conn.execute(
                "SELECT first_seen_at FROM auth_token_observation "
                "WHERE fingerprint=?", ("a" * 64,)
            ).fetchone()[0],
            "2026-08-13 10:00:00",
        )

    def test_auth_failure_is_idempotent_per_run_and_never_stores_token(self) -> None:
        run_id = self.store.start_run("fixture", None, None)
        observe_token_auth_failure(
            self.store, self.cfg, run_id=run_id, run_kind="fixture"
        )
        observe_token_auth_failure(
            self.store, self.cfg, run_id=run_id, run_kind="fixture"
        )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM auth_token_event WHERE event_type='auth_failed'"
            ).fetchone()[0],
            1,
        )
        dump = " ".join(
            str(tuple(row)) for row in self.store.conn.execute(
                "SELECT * FROM auth_token_event"
            )
        )
        self.assertNotIn("fixture-secret-token", dump)

    def test_preflight_refresh_updates_legacy_token_tracker(self) -> None:
        run_id = self.store.start_run("fixture", None, None)
        fp = preflight(
            self.cfg, RefreshingInfoClient(self.cfg), self.store,
            datetime(2026, 8, 13, 11, 0, 0),
            run_id=run_id, run_kind="fixture",
        )
        self.assertEqual(self.cfg.token, "renewed-fixture-token")
        self.assertEqual(self.store.meta_get("token_hash"), fp[:16])
        events = [
            tuple(row) for row in self.store.conn.execute(
                "SELECT fingerprint,event_type FROM auth_token_event "
                "WHERE run_id=? ORDER BY event_type", (run_id,)
            )
        ]
        self.assertIn((fp, "refresh_detected"), events)
        self.assertIn((fp, "probe_ok"), events)
        self.assertIn(
            (
                hashlib.sha256(b"fixture-secret-token").hexdigest(),
                "auth_failed",
            ),
            events,
        )


if __name__ == "__main__":
    unittest.main()
