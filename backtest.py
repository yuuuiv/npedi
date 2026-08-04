"""Helpers for point-in-time data reconstruction and model versioning."""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from typing import Any

from timeseries import TimeseriesStore


def normalize_as_of(value: str | None) -> str | None:
    """Normalize a CLI cutoff to an inclusive UTC ISO timestamp."""
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            dt = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            dt = datetime.combine(dt.date(), time.max, tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(f"as-of 时间无效: {value!r}，请使用 YYYY-MM-DD 或 ISO 时间") from exc
    return dt.isoformat()


def model_version_for_as_of(model_version: str, as_of: str | None) -> str:
    cutoff = normalize_as_of(as_of)
    return f"{model_version}@as_of={cutoff}" if cutoff else model_version


def fact_rows_as_of(store: TimeseriesStore, fact_table: str, as_of: str) -> list[dict[str, Any]]:
    """Return the latest observed version of each fact key at a cutoff."""
    cutoff = normalize_as_of(as_of)
    if not cutoff:
        raise ValueError("fact_rows_as_of 需要 as_of")
    rows = store.conn.execute("""SELECT normalized_json FROM fact_record_version AS v
        WHERE v.fact_table=? AND v.observed_at<=?
          AND v.version_id=(
              SELECT v2.version_id FROM fact_record_version AS v2
              WHERE v2.fact_table=v.fact_table
                AND v2.business_key_hash=v.business_key_hash
                AND v2.observed_at<=?
              ORDER BY v2.observed_at DESC, v2.version_id DESC LIMIT 1
          )""", (fact_table, cutoff, cutoff)).fetchall()
    return [json.loads(row[0]) for row in rows]
