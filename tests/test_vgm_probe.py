from __future__ import annotations

import unittest

from scripts.probe_vgm_batch import _candidate_value, _rows


class VgmProbeTests(unittest.TestCase):
    def test_extracts_frontend_candidate_and_paged_rows(self):
        self.assertEqual(_rows({"data": [{"voyage": "key"}]}), [{"voyage": "key"}])
        self.assertEqual(_rows({"data": {"list": [{"containerNumber": "A"}]}}), [{"containerNumber": "A"}])
        self.assertEqual(_candidate_value({"vesselename": "SHIP / 001", "voyage": "server-key"}), "server-key")


if __name__ == "__main__":
    unittest.main()
