"""Show live, read-only progress and ETA for full container enrichment."""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "npedi.sqlite"
DEFAULT_WINDOW_HOURS = 6.0


def measured_rate(conn: sqlite3.Connection, job: str, window_hours: float) -> tuple[float, int, float]:
    """Return (requests/second, runs, average concurrency) over a wall-clock window.

    Counting work that actually finished is what keeps the ETA stable. The
    earlier estimate multiplied a per-run request rate by however many rows had
    status='running' at sample time, so a worker sitting between batches read as
    idle and roughly doubled the reported ETA.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    requests, runs, busy = conn.execute(
        """SELECT COALESCE(SUM(request_count),0), COUNT(*),
                  COALESCE(SUM((julianday(finished_at)-julianday(started_at))*86400),0)
           FROM crawl_run
           WHERE job_name=? AND status='success' AND finished_at IS NOT NULL
             AND finished_at>=?""",
        (job, since),
    ).fetchone()
    seconds = window_hours * 3600.0
    return int(requests) / seconds, int(runs), float(busy) / seconds


def valid_catalog_size() -> int:
    with sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=30) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM container_enrichment_state WHERE iso6346_valid=1"
        ).fetchone()[0])


def snapshot(valid: int, window_hours: float = DEFAULT_WINDOW_HOURS) -> list[str]:
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
            rate, runs, concurrency = measured_rate(conn, job, window_hours)
            eta_days = pending / rate / 86400 if rate else None
            eta = f"{eta_days:.1f} days" if eta_days is not None else "unknown (no batch finished in window)"
            lines.append(
                f"{label}: {complete:,}/{valid:,} ({complete / valid * 100:.4f}%), "
                f"remaining={pending:,}, errors={errors:,}, "
                f"workers={workers} now/{concurrency:.1f} avg, "
                f"claimed={int(claims.get(kind, 0)):,}, "
                f"speed={rate:.2f} req/s over {window_hours:g}h ({runs} runs), ETA={eta}"
            )
        return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS", help="refresh repeatedly; Ctrl+C stops")
    parser.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help="wall-clock window used to measure throughput; shorten it right after changing worker counts",
    )
    args = parser.parse_args()
    if args.window_hours <= 0:
        parser.error("--window-hours must be positive")
    if args.window_hours < 0.5:
        # A batch runs about seven minutes, and its whole request_count lands on
        # the window it finished in. Windows near that length credit work that
        # was really done earlier, so both speed and concurrency read high.
        print(
            f"Note: a {args.window_hours:g}h window is close to one batch duration; "
            "speed and concurrency will read high. Use 0.5h or more.",
            file=sys.stderr,
        )
    interval = max(5, args.watch) if args.watch else 0
    print("Reading the valid catalog size once; the first result may take a few seconds...", flush=True)
    valid = valid_catalog_size()
    try:
        while True:
            print("\n".join(snapshot(valid, args.window_hours)), flush=True)
            if not interval:
                return 0
            print(f"Next refresh in {interval}s (Ctrl+C to stop viewing; crawlers keep running).", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nProgress viewer stopped; crawlers are still running.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
