from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.gate_anomaly_watchdog import count_sidecar_remaining


class GateAnomalyWatchdogTests(unittest.TestCase):
    def test_counts_every_noncomplete_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sidecar.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE capture_job(status TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO capture_job(status) VALUES(?)",
                [("pending",), ("partial",), ("error",), ("complete",)],
            )
            connection.commit()
            connection.close()
            self.assertEqual(count_sidecar_remaining(path), 3)


if __name__ == "__main__":
    unittest.main()
