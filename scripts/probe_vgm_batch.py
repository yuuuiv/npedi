"""Safely verify whether the VGM UI's vessel key enables batch retrieval."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client import ApiError, AuthExpired, NpediClient
from config import load_config


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    rows = data.get("list") or data.get("rows") or [] if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def _candidate_value(candidate: dict[str, Any]) -> str:
    return str(candidate.get("voyage") or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vessel-name", help="English vessel name; defaults to a frequent local vessel")
    args = parser.parse_args()
    cfg = load_config()

    import sqlite3

    vessel_name = (args.vessel_name or "").strip()
    if not vessel_name:
        with sqlite3.connect(cfg.db_path) as conn:
            row = conn.execute(
                """SELECT TRIM(vessel),COUNT(*) AS n FROM gate_events
                   WHERE vessel IS NOT NULL AND LENGTH(TRIM(vessel))>4
                   GROUP BY TRIM(vessel) ORDER BY n DESC LIMIT 1"""
            ).fetchone()
        vessel_name = row[0] if row else ""
    if not vessel_name:
        print("No vessel name is available for the probe.", file=sys.stderr)
        return 1

    try:
        with NpediClient(cfg) as client:
            candidates = _rows(client.vgm_vessel_info(vessel_name.upper()))
            usable = [item for item in candidates if _candidate_value(item)]
            if not usable:
                print(json.dumps({"vessel_name": vessel_name, "candidates": 0, "batch_filter": False}))
                return 2
            selected = usable[0]
            payload = client.vgm_page(1, page_size=min(cfg.page_size, 200), vessel=_candidate_value(selected))
            rows = _rows(payload)
            data = payload.get("data") or {}
            total = data.get("total") if isinstance(data, dict) else len(rows)
    except AuthExpired as exc:
        print(f"VGM probe stopped: {exc}", file=sys.stderr)
        return 3
    except ApiError as exc:
        print(f"VGM probe failed: {exc}", file=sys.stderr)
        return 4

    print(json.dumps({
        "vessel_name": vessel_name,
        "candidates": len(usable),
        "selected_label": selected.get("vesselename"),
        "selected_voyage_key": _candidate_value(selected),
        "first_page_rows": len(rows),
        "reported_total": total,
        "batch_filter": bool(rows) or (isinstance(total, int) and total == 0),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
