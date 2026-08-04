PRAGMA foreign_keys = ON;

-- Append-only observations for facts whose current projection is an upsert.
-- `observed_at` is when the crawler saw the record, not the business event time.
CREATE TABLE IF NOT EXISTS fact_record_version (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_table TEXT NOT NULL,
    business_key_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    event_time TEXT,
    record_hash TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '',
    UNIQUE(fact_table, business_key_hash, observed_at, record_hash)
);
CREATE INDEX IF NOT EXISTS idx_fact_record_version_lookup
    ON fact_record_version(fact_table, business_key_hash, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_fact_record_version_asof
    ON fact_record_version(fact_table, observed_at, event_time);

-- Historical Gold projections. The existing agg_* tables remain the current
-- projection; these tables keep independent results for each as-of cutoff.
CREATE TABLE IF NOT EXISTS agg_flow_daily_asof (
    as_of_time TEXT NOT NULL, flow_date TEXT NOT NULL, terminal_code TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT '', route_key TEXT NOT NULL DEFAULT '', cargo_group_key TEXT NOT NULL DEFAULT '',
    vessel_operator TEXT NOT NULL DEFAULT '', vgm_container_count INTEGER NOT NULL DEFAULT 0,
    vgm_weight_kg REAL NOT NULL DEFAULT 0, released_bill_count INTEGER NOT NULL DEFAULT 0,
    released_weight REAL NOT NULL DEFAULT 0, released_volume REAL NOT NULL DEFAULT 0,
    transshipment_container_count INTEGER NOT NULL DEFAULT 0, transshipment_weight REAL NOT NULL DEFAULT 0,
    transshipment_volume REAL NOT NULL DEFAULT 0, planned_vessel_call_count INTEGER NOT NULL DEFAULT 0,
    actual_vessel_call_count INTEGER NOT NULL DEFAULT 0, avg_arrival_delay_hours REAL,
    avg_departure_delay_hours REAL, created_at TEXT NOT NULL,
    PRIMARY KEY(as_of_time, flow_date, terminal_code, direction, route_key, cargo_group_key, vessel_operator)
);

CREATE TABLE IF NOT EXISTS agg_flow_weekly_asof (
    as_of_time TEXT NOT NULL, week_start TEXT NOT NULL, terminal_code TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT '', route_key TEXT NOT NULL DEFAULT '', cargo_group_key TEXT NOT NULL DEFAULT '',
    vessel_operator TEXT NOT NULL DEFAULT '', vgm_container_count INTEGER NOT NULL DEFAULT 0,
    vgm_weight_kg REAL NOT NULL DEFAULT 0, released_bill_count INTEGER NOT NULL DEFAULT 0,
    released_weight REAL NOT NULL DEFAULT 0, released_volume REAL NOT NULL DEFAULT 0,
    transshipment_container_count INTEGER NOT NULL DEFAULT 0, transshipment_weight REAL NOT NULL DEFAULT 0,
    transshipment_volume REAL NOT NULL DEFAULT 0, planned_vessel_call_count INTEGER NOT NULL DEFAULT 0,
    actual_vessel_call_count INTEGER NOT NULL DEFAULT 0, avg_arrival_delay_hours REAL,
    avg_departure_delay_hours REAL, created_at TEXT NOT NULL,
    PRIMARY KEY(as_of_time, week_start, terminal_code, direction, route_key, cargo_group_key, vessel_operator)
);
