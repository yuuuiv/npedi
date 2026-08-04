from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aggregate import rebuild_gold
from cluster import build_feature_windows, cluster_features
from curves import build_curves
from timeseries import TimeseriesStore


VGM_FIELDS = (
    "business_key_hash", "container_no", "vessel_code", "vessel_name_raw", "voyage",
    "terminal_code", "operator_code", "direction", "container_type", "vgm_weight_kg",
    "vgm_method", "operator_time", "terminal_received_time", "result_code",
    "result_description", "sender_code", "receiver_code", "ingested_at", "record_hash", "raw_json",
)


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "backtest.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _vgm(self, weight: float, observed_at: str, record_hash: str) -> dict[str, str | float | None]:
        return {
            "business_key_hash": "same-container-event",
            "container_no": "ABCU1234567",
            "vessel_code": "V1",
            "vessel_name_raw": "TEST",
            "voyage": "V001",
            "terminal_code": "T1",
            "operator_code": "OP",
            "direction": "E",
            "container_type": "40HC",
            "vgm_weight_kg": weight,
            "vgm_method": "M",
            "operator_time": "2026-01-10T00:00:00+00:00",
            "terminal_received_time": None,
            "result_code": "Y",
            "result_description": None,
            "sender_code": None,
            "receiver_code": None,
            "ingested_at": observed_at,
            "record_hash": record_hash,
            "raw_json": json.dumps({"weight": weight}),
        }

    def test_as_of_uses_the_version_visible_at_cutoff(self):
        with TimeseriesStore(self.db) as store:
            first = self._vgm(100.0, "2026-01-11T00:00:00+00:00", "hash-1")
            second = self._vgm(200.0, "2026-02-11T00:00:00+00:00", "hash-2")
            store.upsert_fact("fact_container_vgm", first, VGM_FIELDS)
            store.upsert_fact("fact_container_vgm", second, VGM_FIELDS)

            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM fact_record_version").fetchone()[0], 2)
            rebuild_gold(store, as_of="2026-01-31")
            old_weight = store.conn.execute("SELECT vgm_weight_kg FROM agg_flow_daily_asof WHERE as_of_time LIKE '2026-01-31%'").fetchone()[0]
            self.assertEqual(old_weight, 100.0)

            rebuild_gold(store, as_of="2026-03-01")
            new_weight = store.conn.execute("SELECT vgm_weight_kg FROM agg_flow_daily_asof WHERE as_of_time LIKE '2026-03-01%'").fetchone()[0]
            self.assertEqual(new_weight, 200.0)

    def test_as_of_curves_and_cluster_keep_model_versions_separate(self):
        with TimeseriesStore(self.db) as store:
            store.conn.execute("""INSERT INTO agg_flow_daily_asof
                (as_of_time,flow_date,terminal_code,direction,vgm_weight_kg,created_at)
                VALUES('2026-01-31T23:59:59.999999+00:00','2026-01-10','T1','E',100,'now')""")
            store.conn.execute("""INSERT INTO agg_flow_weekly_asof
                (as_of_time,week_start,terminal_code,direction,vgm_weight_kg,created_at)
                VALUES('2026-01-31T23:59:59.999999+00:00','2026-01-05','T1','E',100,'now')""")
            store.conn.commit()

            self.assertGreater(build_curves(store, granularity="week", as_of="2026-01-31"), 0)
            features = build_feature_windows(store, curve_type="vgm", granularity="week", as_of="2026-01-31")
            self.assertEqual(features[0]["feature_version"], "v1@as_of=2026-01-31T23:59:59.999999+00:00")
            result = cluster_features(store, features, as_of="2026-01-31")
            params = json.loads(store.conn.execute("SELECT parameters_json FROM cluster_run WHERE cluster_run_id=?", (result["cluster_run_id"],)).fetchone()[0])
            self.assertEqual(params["as_of"], "2026-01-31T23:59:59.999999+00:00")

    def test_plan_as_of_uses_natural_identity_when_old_key_includes_eta(self):
        with TimeseriesStore(self.db) as store:
            store.conn.execute("""INSERT INTO fact_vessel_plan_snapshot
                (business_key_hash,vessel_code,voyage,terminal_code,direction,eta,snapshot_time,record_hash,raw_json)
                VALUES('old-key-1','V1','V001','T1','E','2026-01-20T00:00:00+00:00','2026-01-01T00:00:00+00:00','hash-1','{}')""")
            store.conn.execute("""INSERT INTO fact_vessel_plan_snapshot
                (business_key_hash,vessel_code,voyage,terminal_code,direction,eta,snapshot_time,record_hash,raw_json)
                VALUES('old-key-2','V1','V001','T1','E','2026-02-20T00:00:00+00:00','2026-02-01T00:00:00+00:00','hash-2','{}')""")
            store.conn.commit()

            rebuild_gold(store, as_of="2026-01-31")
            self.assertEqual(store.conn.execute("SELECT SUM(planned_vessel_call_count) FROM agg_flow_daily_asof WHERE as_of_time LIKE '2026-01-31%'").fetchone()[0], 1)
            rebuild_gold(store, as_of="2026-02-28")
            self.assertEqual(store.conn.execute("SELECT SUM(planned_vessel_call_count) FROM agg_flow_daily_asof WHERE as_of_time LIKE '2026-02-28%'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
