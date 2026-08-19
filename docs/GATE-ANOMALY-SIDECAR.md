# Gate anomaly sidecar

`gate_anomaly_sidecar.py` preserves CODECO responses for virtual vessels and
operational aggregate buckets without merging them into the physical
`gate_events` dataset.

## Data separation

- `npedi.sqlite` keeps the original plan evidence and an exact
  `(vesselcode, voyage)` entry in `gate_history_rejected_pair`. This excludes
  the pair from normal history work and downstream physical-vessel reads.
- `gate_anomaly_sidecar.sqlite` stores the query identity, page checkpoints,
  response metadata, and every response row as canonical raw JSON.
- Response identity is never filled from the query. A missing response vessel
  code remains `NULL`, so an ignored remote filter cannot silently relabel
  unrelated rows.
- Existing main-database rows are retained. Normal readers exclude their exact
  quarantined pair; they are not deleted or rewritten as empty results.

## Discovery policy

Automatic semantic classification requires both:

1. an explicit non-vessel marker such as virtual vessel, transshipment bucket,
   empty-return bucket, terminal transfer, temporary drop, workface, crane
   repair, or copper-cargo bucket; and
2. no valid seven-digit IMO and no valid nine-digit MMSI.

Separately, a probed but unimported pair with at least 10,000 returned rows is
classified as an unvalidated total outlier. The classification is exact-pair
scoped; a vessel code is never globally denied because it may be reused by a
real voyage in another period.

## Commands

Discovery and status are local-only:

```powershell
.\.venv\Scripts\python.exe .\gate_anomaly_sidecar.py discover `
  --eta-start 2023-01-01 --eta-end 2026-06-30 `
  --total-threshold 10000 --page-cap 20000

.\.venv\Scripts\python.exe .\gate_anomaly_sidecar.py quarantine-main
.\.venv\Scripts\python.exe .\gate_anomaly_sidecar.py status --json
```

Remote capture is sequential and bounded per invocation:

```powershell
.\.venv\Scripts\python.exe .\gate_anomaly_sidecar.py capture `
  --max-requests 500 --delay-ms 1100 --auto-login
```

Each page and its `next_page` checkpoint commit in one SQLite transaction.
Subsequent invocations resume rather than replaying the direction. The sidecar
has an explicit 20,000-page job cap and does not use the normal pipeline's
5,000-page iterator.

## Total drift

These buckets keep accumulating while a multi-hour job walks them, so the
reported total is expected to grow between page 1 and the last page. The job
therefore captures a snapshot rather than chasing a moving tail:

- `first_total` freezes the page plan. `expected_pages` and job completion are
  derived from it, so rows appended after page 1 fall outside the window and
  are left for a later job.
- A total that grows is accepted and recorded in `last_total`; the drift for
  any job is `last_total - first_total`.
- A total that *shrinks* below `first_total` still fails the job. That is a
  changed or re-keyed result set, not accumulation.
- Per-page row counts are still checked against the live total, so a short page
  inside the frozen window remains a gap and still fails.

Resuming an errored job requires `--retry-errors`; the supervisor sets it
automatically when `status --json` reports any error job. Resume continues from
`max(next_page, MAX(capture_page.page_no) + 1)` and never refetches saved pages.

`run_gate_anomaly_sidecar.ps1` is the unattended supervisor. It waits for the
normal history wrapper PID and verifies that the normal queue is zero before
making any remote request. `scripts/gate_anomaly_watchdog.py` monitors that
supervisor and emails the configured recipient if it exits or loses its log
heartbeat while sidecar jobs remain.
