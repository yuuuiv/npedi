from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from aggregate import rebuild_gate_daily
from coverage import iso6346_valid, seed_container_catalog
from timeseries import TimeseriesStore


class CoverageTests(unittest.TestCase):
    def test_iso6346_check_digit(self):
        self.assertTrue(iso6346_valid("CSQU3054383"))
        self.assertTrue(iso6346_valid("ABCU0000017"))
        self.assertFalse(iso6346_valid("ABCU0000018"))
        self.assertFalse(iso6346_valid("not-a-container"))

    def test_gate_history_seeds_full_catalog_and_aggregates(self):
        with tempfile.TemporaryDirectory() as folder:
            with TimeseriesStore(Path(folder) / "coverage.sqlite") as store:
                store.conn.execute("""CREATE TABLE gate_events(
                    id TEXT PRIMARY KEY, type TEXT, vesselcode TEXT, voyage TEXT,
                    direct TEXT, ctnNo TEXT, blNo TEXT, ctnSizeType TEXT, msgReceiveTime TEXT,
                    inGateTime TEXT, outGateTime TEXT, fetched_at TEXT NOT NULL
                )""")
                store.conn.execute("""INSERT INTO gate_events VALUES(
                    '1','GATE_IN','UN1','V1','E','CSQU3054383','BL1','45GP',
                    '20260801120000','20260801110000','20260801130000',
                    '2026-08-02T00:00:00+00:00')""")
                # GATE_OUT carries the historical inGateTime as well. It must
                # contribute only an OUT event, not synthesize a second IN.
                store.conn.execute("""INSERT INTO gate_events VALUES(
                    '2','GATE_OUT','UN1','V1','I','CSQU3054383','BL1','45GP',
                    '20260801140000','20260801110000','20260801130000',
                    '2026-08-02T00:00:00+00:00')""")
                store.conn.commit()

                result = seed_container_catalog(store)
                self.assertEqual(result["valid"], 1)
                self.assertEqual(rebuild_gate_daily(store), 2)
                self.assertEqual(
                    tuple(store.conn.execute(
                        "SELECT SUM(in_gate_container_count),SUM(out_gate_container_count),MAX(unique_gate_container_count),SUM(in_gate_teu),SUM(out_gate_teu) FROM agg_gate_daily"
                    ).fetchone()),
                    (1, 1, 1, 2.0, 2.0),
                )
                self.assertEqual(
                    store.conn.execute("SELECT COUNT(*) FROM container_event_full").fetchone()[0],
                    2,
                )


if __name__ == "__main__":
    unittest.main()
