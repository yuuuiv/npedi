from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from change import detect_changes, snapshot_trends
from cluster import build_feature_windows, cluster_features
from curves import build_curves
from render import render_curves
from timeseries import TimeseriesStore, now_utc


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "analytics.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_feature_cluster_change_and_plotly_artifact(self):
        with TimeseriesStore(self.db) as store:
            for i in range(8):
                week = f"2026-01-{5 + i * 7:02d}"
                for entity, base in (("T1:E", 1.0), ("T2:E", 10.0), ("T3:E", 20.0)):
                    store.conn.execute("INSERT INTO mart_curve_series(curve_id,curve_type,entity_type,entity_key,granularity,time_bucket,value,quality_flag,model_version,computed_at,source_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"vgm:{entity}", "vgm", "terminal", entity, "week", week, base + i, "complete", "v1", now_utc(), '{"completeness_ratio":1.0}'))
            store.conn.commit()
            features = build_feature_windows(store, curve_type="vgm", granularity="week", min_completeness=.8)
            self.assertEqual(len(features), 3)
            result = cluster_features(store, features, algorithm="hierarchical", min_completeness=.8)
            self.assertEqual(result["sample_count"], 3)
            self.assertGreaterEqual(result["cluster_count"], 2)
            self.assertGreater(detect_changes(store, threshold=1.0)["anomalies"], 0)
            self.assertGreater(snapshot_trends(store), 0)
            output = render_curves(store, Path(self.tmp.name) / "curves.html")
            self.assertTrue(output.exists())
            html = output.read_text(encoding="utf-8")
            self.assertIn("Plotly.newPlot", html)
            self.assertIn("历史补爬和覆盖验收完成前暂停展示", html)
            self.assertIn('"gate_history_validated": false', html)
            self.assertIn('"gate": []', html)

    def test_data_insufficient_is_not_forced_into_a_cluster(self):
        with TimeseriesStore(self.db) as store:
            result = cluster_features(store, [{"entity_type": "terminal", "entity_key": "T1", "feature_version": "v1", "window_start": "2026-01-01", "window_end": "2026-01-01", "data_completeness": 1.0}], algorithm="hierarchical")
            self.assertEqual(result["cluster_count"], 0)


if __name__ == "__main__":
    unittest.main()
