PRAGMA foreign_keys = ON;

-- vesselList is a point-in-time directory, not a complete historical index.
-- Keep plan-derived candidates separate until a CODECO point query proves
-- that a vessel/voyage pair has data. This prevents empty probes from
-- polluting gate_voyages and makes the one-worker backfill resumable.
CREATE TABLE IF NOT EXISTS gate_history_candidate (
    vesselcode       TEXT NOT NULL,
    voyage           TEXT NOT NULL,
    vesselename      TEXT,
    first_eta        TEXT NOT NULL,
    last_eta         TEXT NOT NULL,
    source_plan_rows INTEGER NOT NULL DEFAULT 0,
    vessel_in_catalog INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','hit','empty','complete')),
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    probed_at        TEXT,
    gatein_total     INTEGER,
    gateout_total    INTEGER,
    discovered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (vesselcode, voyage)
);

CREATE INDEX IF NOT EXISTS idx_gate_history_candidate_pending
    ON gate_history_candidate(status, last_eta DESC, vesselcode, voyage);

CREATE TABLE IF NOT EXISTS gate_history_candidate_month (
    vesselcode       TEXT NOT NULL,
    voyage           TEXT NOT NULL,
    eta_month        TEXT NOT NULL,
    vesselename      TEXT,
    first_eta        TEXT NOT NULL,
    last_eta         TEXT NOT NULL,
    source_plan_rows INTEGER NOT NULL DEFAULT 0,
    discovered_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (vesselcode, voyage, eta_month)
);

CREATE INDEX IF NOT EXISTS idx_gate_history_candidate_month_scope
    ON gate_history_candidate_month(eta_month, vesselcode, voyage);

CREATE TABLE IF NOT EXISTS gate_history_seed_scope (
    eta_start          TEXT NOT NULL,
    eta_end_exclusive  TEXT NOT NULL,
    known_vessels_only INTEGER NOT NULL,
    seeded_at          TEXT NOT NULL,
    PRIMARY KEY (eta_start, eta_end_exclusive, known_vessels_only)
);
