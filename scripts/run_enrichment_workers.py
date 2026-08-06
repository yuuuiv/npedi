"""Run a bounded set of crash-safe remote enrichment workers."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_config
from timeseries import TimeseriesStore


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "status" in value:
            return value
    raise RuntimeError("worker did not emit a crawl result")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=("vgm", "history"))
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5), metavar="1-4")
    parser.add_argument("--batch-size", type=int, default=500, choices=range(1, 2001), metavar="1-2000")
    parser.add_argument("--max-batches-per-worker", type=int, default=0)
    args = parser.parse_args()
    if not PYTHON.is_file():
        parser.error(f"missing crawler interpreter: {PYTHON}")

    stop = threading.Event()
    target = "container-history" if args.target == "history" else "vgm"

    # Apply migrations once before child processes race to open the database.
    with TimeseriesStore(load_config().db_path):
        pass

    def worker(index: int) -> int:
        batches = 0
        claim_id = f"{args.target}-{index}-{uuid.uuid4().hex}"
        while not stop.is_set():
            if args.max_batches_per_worker and batches >= args.max_batches_per_worker:
                return batches
            command = [
                str(PYTHON), "npedi.py", "crawl", target,
                "--limit", str(args.batch_size), "--offset", "0", "--resume",
                "--claim-id", claim_id,
            ]
            result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                stop.set()
                detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
                raise RuntimeError(f"worker {index} exited {result.returncode}: {' '.join(detail)}")
            payload = _last_json(result.stdout)
            if payload.get("status") != "success":
                stop.set()
                raise RuntimeError(f"worker {index} crawl status={payload.get('status')}")
            claimed = int(payload.get("claimed") or 0)
            if claimed == 0:
                return batches
            batches += 1
            print(
                f"worker={index} batch={batches} claimed={claimed} "
                f"requests={payload.get('requests')} seen={payload.get('seen')}",
                flush=True,
            )
        return batches

    print(f"Starting {args.workers} {args.target} workers; batch size={args.batch_size}.")
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(worker, index + 1) for index in range(args.workers)]
            completed = sum(future.result() for future in as_completed(futures))
    except (KeyboardInterrupt, RuntimeError) as exc:
        stop.set()
        print(f"Enrichment workers stopped: {exc}", file=sys.stderr)
        return 1
    print(f"All available {args.target} rows finished; worker batches={completed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
