PRAGMA foreign_keys = ON;

-- Secret-safe, low-frequency WebToken lifetime observations.  The
-- fingerprint is SHA-256(token); token material and response bodies are never
-- persisted here.
CREATE TABLE IF NOT EXISTS auth_token_observation (
    fingerprint           TEXT PRIMARY KEY,
    first_seen_at          TEXT NOT NULL,
    first_success_at       TEXT,
    last_success_at        TEXT,
    last_auth_failure_at   TEXT,
    successful_preflights INTEGER NOT NULL DEFAULT 0,
    successful_runs       INTEGER NOT NULL DEFAULT 0,
    successful_requests   INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_token_event (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key      TEXT NOT NULL UNIQUE,
    fingerprint    TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    event_type     TEXT NOT NULL
                   CHECK(event_type IN (
                       'probe_ok','run_ok','auth_failed','refresh_detected'
                   )),
    run_id         INTEGER,
    run_kind       TEXT,
    endpoint       TEXT,
    http_status    INTEGER,
    api_code       INTEGER,
    request_count  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_auth_token_event_time
    ON auth_token_event(observed_at, event_type);

-- Plan keys that are unsafe to send to the exact-match endpoint are retained
-- as audit evidence, not mislabeled as remote empty results.
CREATE TABLE IF NOT EXISTS gate_history_rejected_pair (
    vesselcode       TEXT NOT NULL,
    voyage           TEXT NOT NULL,
    reason           TEXT NOT NULL,
    vesselename      TEXT,
    first_eta        TEXT NOT NULL,
    last_eta         TEXT NOT NULL,
    source_plan_rows INTEGER NOT NULL DEFAULT 0,
    discovered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (vesselcode, voyage)
);

CREATE INDEX IF NOT EXISTS idx_gate_history_rejected_reason
    ON gate_history_rejected_pair(reason, last_eta DESC);
