"""Email once if the anomaly-sidecar supervisor disconnects before completion."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_config
from scripts.history_backfill_watchdog import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_STALE_SECONDS,
    DEFAULT_TO,
    HistoryBackfillWatchdog,
    WatchSpec,
    WatchdogError,
)


log = logging.getLogger("npedi.gate_anomaly_watchdog")


def count_sidecar_remaining(path: Path) -> int:
    database = path.resolve()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM capture_job WHERE status<>'complete'"
        ).fetchone()
        return int(row[0])
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor the gate anomaly sidecar and email on disconnect."
    )
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--sidecar-db", required=True, type=Path)
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--eta-start", default="2023-01-01")
    parser.add_argument("--eta-end", default="2026-06-30")
    parser.add_argument("--to", dest="recipient", default=DEFAULT_TO)
    parser.add_argument("--stale-seconds", type=float, default=3600.0)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    spec = WatchSpec(
        pid=args.pid,
        eta_start=args.eta_start,
        eta_end=args.eta_end,
        # The shared sender accepts this stable value; the email still includes
        # the sidecar-specific log/state paths and exact remaining job count.
        catalog_scope="all",
        log_file=args.log_file,
        state_file=args.state_file,
        recipient=args.recipient,
        stale_seconds=args.stale_seconds or DEFAULT_STALE_SECONDS,
        poll_seconds=args.poll_seconds or DEFAULT_POLL_SECONDS,
    )
    try:
        watchdog = HistoryBackfillWatchdog(
            load_config(),
            spec,
            remaining_reader=lambda: count_sidecar_remaining(args.sidecar_db),
        )
        return watchdog.run()
    except WatchdogError as exc:
        log.error("旁路看门狗启动失败：%s", exc)
        return 1
    except KeyboardInterrupt:
        log.info("旁路看门狗已停止")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
