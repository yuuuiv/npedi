"""Prove that an as-of reconstruction returns the same thing days later.

Why this works. Every as-of path filters on an *ingestion* timestamp, never on a
timestamp taken from the source payload:

    fact_record_version.observed_at   <- normalize.py sets ingested_at=now_utc()
    fact_vessel_plan_snapshot.snapshot_time <- timeseries.py sets now_utc()
    gate_events.fetched_at            <- set when the page was fetched

and fact_record_version is append-only (no UPDATE/DELETE anywhere in the tree,
INSERT OR IGNORE against UNIQUE(fact_table,business_key_hash,observed_at,
record_hash)). So the set of rows visible at cutoff T can only be added to
*after* T; it can never change retroactively.

This script turns that argument into evidence. It fingerprints the exact input
set each as-of query would read, so a later run either reproduces the hash byte
for byte or names the table that moved.

    python scripts/verify_reproducible.py --record          # today
    python scripts/verify_reproducible.py --verify docs/reproducibility/<file>.json

What it does NOT claim: that an as-of cutoff shows everything that had happened
in the world by then. It shows what had been *observed* by then. The backfill is
still running, so a container crawled tomorrow lands with observed_at=tomorrow
even though its events are months old. That is correct point-in-time behaviour
and it is exactly what makes the result stable, but it means an early cutoff is
sparse, not wrong.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest import normalize_as_of
from config import load_config

VERSIONED_TABLES = (
    "fact_container_vgm",
    "fact_cargo_release",
    "fact_transshipment",
    "fact_container_event",
)
DEFAULT_DIR = PROJECT_ROOT / "docs" / "reproducibility"


def _stream_hash(cursor) -> tuple[int, str]:
    """SHA-256 over an ordered cursor, without materialising it."""
    digest = hashlib.sha256()
    rows = 0
    for row in cursor:
        digest.update(("\x1f".join("" if v is None else str(v) for v in row) + "\x1e").encode("utf-8"))
        rows += 1
    return rows, digest.hexdigest()


def _tally(conn: sqlite3.Connection, table: str, key: str, time_col: str,
           where: str, args: tuple, *, text_key: bool = False) -> dict:
    """Index-only summary of a row set.

    Where the key is INTEGER PRIMARY KEY it is the rowid, which SQLite keeps in
    every index, so count/min/max/sum come from an index scan without reading
    the rows. Any deletion, rewrite or mid-sequence insertion moves the sum.

    gate_events.id is TEXT (a 23-digit id assigned by the source). SUM() would
    coerce that to REAL and the result would depend on accumulation order, so
    sum the last nine digits as an integer instead — exact, and still shifts if
    any row changes.

    The timestamps are summed as epoch seconds as well. Counts and id sums alone
    miss the tampering that matters most here: moving a row's observed_at
    *earlier* while leaving it inside the cutoff keeps both unchanged, yet it
    silently rewrites what an as-of query returns.
    """
    total = (f"COALESCE(SUM(CAST(substr({key},-9) AS INTEGER)),0)" if text_key
             else f"COALESCE(SUM({key}),0)")
    row = conn.execute(
        f"SELECT COUNT(*), MIN({key}), MAX({key}), {total},"
        f" COALESCE(SUM(CAST(strftime('%s',{time_col}) AS INTEGER)),0),"
        f" MAX({time_col})"
        f" FROM {table} WHERE {where}",
        args,
    ).fetchone()
    return {"rows": row[0], "min_id": row[1], "max_id": row[2], "sum_id": row[3],
            "sum_epoch": row[4], "max_time": row[5]}


def fingerprint(conn: sqlite3.Connection, cutoff: str, *, strict: bool) -> dict:
    """Summarise (or hash) the exact input set every as-of query reads."""
    out: dict[str, dict] = {}
    has_gate = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gate_events'"
    ).fetchone())

    sources: list[tuple[str, str, str, str, str, tuple, str, bool]] = [
        (f"version:{t}", "fact_record_version", "version_id", "observed_at",
         "fact_table=? AND observed_at<=?", (t, cutoff),
         "version_id, business_key_hash, observed_at, record_hash", False)
        for t in VERSIONED_TABLES
    ]
    sources.append((
        "plan:fact_vessel_plan_snapshot", "fact_vessel_plan_snapshot",
        "vessel_plan_key", "snapshot_time", "snapshot_time<=?", (cutoff,),
        "vessel_plan_key, business_key_hash, snapshot_time, record_hash", False,
    ))
    if has_gate:
        sources.append((
            "gate:gate_events", "gate_events", "id", "fetched_at",
            "fetched_at<=?", (cutoff,),
            "id, ctnNo, inGateTime, outGateTime, fetched_at", True,
        ))

    for label, table, key, time_col, where, args, columns, text_key in sources:
        entry = _tally(conn, table, key, time_col, where, args, text_key=text_key)
        if strict:
            # Full content hash. Reads every row, so it competes with the
            # crawlers for I/O on a database this size — run it when it matters,
            # not on every check.
            _, digest = _stream_hash(conn.execute(
                f"SELECT {columns} FROM {table} WHERE {where} ORDER BY {key}", args
            ))
            entry["sha256"] = digest
        out[label] = entry
    return out


def build(conn: sqlite3.Connection, cutoffs: list[str], *, strict: bool) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "strict" if strict else "fast",
        "db": str(load_config().db_path),
        "cutoffs": {c: fingerprint(conn, c, strict=strict) for c in cutoffs},
    }


def compare(old: dict, new: dict) -> int:
    problems = 0
    print(f"baseline recorded at {old['generated_at']}  (mode={old.get('mode', 'strict')})")
    print(f"verified  now        {new['generated_at']}\n")
    for cutoff, before in old["cutoffs"].items():
        after = new["cutoffs"].get(cutoff)
        if after is None:
            print(f"  as-of {cutoff}: MISSING from this run")
            problems += 1
            continue
        for key, prev in before.items():
            cur = after.get(key, {})
            fields = [f for f in ("rows", "min_id", "max_id", "sum_id",
                                  "sum_epoch", "max_time", "sha256")
                      if f in prev and f in cur]
            moved = [f for f in fields if prev[f] != cur[f]]
            flag = "same" if not moved else "CHANGED: " + ", ".join(
                f"{f} {prev[f]} -> {cur[f]}" if f != "sha256" else "sha256" for f in moved
            )
            print(f"  as-of {cutoff}  {key:38} {prev['rows']:>10,} rows  {flag}")
            if moved:
                problems += 1
    print()
    if problems:
        print(f"FAIL: {problems} fingerprint(s) changed — history at these cutoffs was rewritten.")
    else:
        print("PASS: every as-of input set matches the baseline.")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", action="store_true", help="write a new baseline")
    group.add_argument("--verify", type=Path, metavar="BASELINE", help="re-check an earlier baseline")
    parser.add_argument("--as-of", action="append", default=None,
                        help="cutoff (YYYY-MM-DD or ISO); repeatable. Default: today and the 3 preceding days")
    parser.add_argument("--mode", choices=("fast", "strict"), default="fast",
                        help="fast: index-only counts and id sums, seconds. "
                             "strict: adds a full content hash, minutes, and competes "
                             "with the crawlers for I/O")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--db", type=Path, default=None,
                        help="database to read; defaults to DB_PATH. Point it at a backup "
                             "to check that the copy carries the same history.")
    args = parser.parse_args()

    db = Path(args.db) if args.db else Path(load_config().db_path)
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=300)

    if args.verify:
        old = json.loads(args.verify.read_text(encoding="utf-8"))
        # Verify in whatever mode the baseline was recorded in, or the run would
        # compare fields the baseline never captured.
        new = build(conn, list(old["cutoffs"]), strict=old.get("mode") == "strict")
        conn.close()
        return compare(old, new)

    # Default cutoffs must all lie in the past. End-of-today is a *future*
    # instant, so rows keep landing under it and a later verify would report a
    # change that is not a rewrite. "Now" is safe: nothing can later appear with
    # an earlier observed_at.
    now = datetime.now(timezone.utc)
    raw = args.as_of or (
        [now.isoformat()]
        + [(now.date() - timedelta(days=n)).isoformat() for n in (1, 2, 3)]
    )
    cutoffs = [normalize_as_of(value) for value in raw]
    future = [c for c in cutoffs if c > now.isoformat()]
    if future:
        print(f"warning: cutoff(s) in the future will keep changing until they pass: {future}",
              file=sys.stderr)

    report = build(conn, cutoffs, strict=args.mode == "strict")
    conn.close()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"baseline-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for cutoff, parts in report["cutoffs"].items():
        print(f"as-of {cutoff}")
        for key, value in parts.items():
            mark = value.get("sha256", "")[:16] or f"sum_id={value['sum_id']}"
            print(f"  {key:38} {value['rows']:>10,} rows  {mark}")
    print(f"\nbaseline written to {path}")
    print("re-run later with:")
    print(f"  python scripts/verify_reproducible.py --verify {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
