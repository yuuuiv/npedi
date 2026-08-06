"""Show live, read-only progress and ETA for full container enrichment."""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "npedi.sqlite"


def _duration_seconds(started: str, finished: str) -> float:
    return max(0.0, (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds())


def valid_catalog_size() -> int:
    with sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM container_enrichment_state WHERE iso6346_valid=1"
        ).fetchone()[0])


def snapshot(valid: int) -> list[str]:
    with sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30) as conn:
        claims = dict(conn.execute(
            "SELECT kind,COUNT(*) FROM container_enrichment_claim GROUP BY kind"
        ))
        lines = [datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")]
        for label, kind, column, job in (
            ("VGM", "vgm", "vgm_status", "vgm"),
            ("container-history", "history", "history_status", "container_history"),
        ):
            complete = int(conn.execute(
                f"""SELECT COUNT(*) FROM container_enrichment_state
                    WHERE {column}='complete' AND iso6346_valid=1"""
            ).fetchone()[0])
            errors = int(conn.execute(
                f"""SELECT COUNT(*) FROM container_enrichment_state
                    WHERE {column}='error' AND iso6346_valid=1"""
            ).fetchone()[0])
            pending = valid - complete - errors
            workers = conn.execute(
                "SELECT COUNT(*) FROM crawl_run WHERE job_name=? AND status='running'",
                (job,),
            ).fetchone()[0]
            samples = conn.execute(
                """SELECT started_at,finished_at,request_count FROM crawl_run
                   WHERE job_name=? AND status='success' AND request_count>=100
                     AND finished_at IS NOT NULL
                   ORDER BY finished_at DESC LIMIT 12""",
                (job,),
            ).fetchall()
            requests = sum(int(row[2]) for row in samples)
            seconds = sum(_duration_seconds(row[0], row[1]) for row in samples)
            per_worker_rate = requests / seconds if seconds else 0.0
            aggregate_rate = per_worker_rate * workers
            eta_days = pending / aggregate_rate / 86400 if aggregate_rate else None
            eta = f"{eta_days:.1f} days" if eta_days is not None else "unknown"
            lines.append(
                f"{label}: {complete:,}/{valid:,} ({complete / valid * 100:.4f}%), "
                f"remaining={pending:,}, errors={errors:,}, workers={workers}, "
                f"claimed={int(claims.get(kind, 0)):,}, speed={aggregate_rate:.2f} req/s, ETA={eta}"
            )
        return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS", help="refresh repeatedly; Ctrl+C stops")
    args = parser.parse_args()
    interval = max(5, args.watch) if args.watch else 0
    print("Reading the valid catalog size once; the first result may take a few seconds...", flush=True)
    valid = valid_catalog_size()
    try:
        while True:
            print("\n".join(snapshot(valid)), flush=True)
            if not interval:
                return 0
            print(f"Next refresh in {interval}s (Ctrl+C to stop viewing; crawlers keep running).", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nProgress viewer stopped; crawlers are still running.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
