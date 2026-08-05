PRAGMA foreign_keys = ON;

-- One row per container discovered anywhere in the local port data. VGM and
-- single-container history are independent remote enrichments: completing one
-- must never make the other disappear from its queue.
CREATE TABLE IF NOT EXISTS container_enrichment_state (
    container_no TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    first_event_time TEXT,
    last_event_time TEXT,
    gate_event_count INTEGER NOT NULL DEFAULT 0,
    iso6346_valid INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    vgm_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(vgm_status IN ('pending','complete','error','invalid')),
    vgm_attempt_count INTEGER NOT NULL DEFAULT 0,
    vgm_last_attempt_at TEXT,
    vgm_last_error TEXT,
    history_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(history_status IN ('pending','complete','error','invalid')),
    history_attempt_count INTEGER NOT NULL DEFAULT 0,
    history_last_attempt_at TEXT,
    history_last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_container_state_vgm
    ON container_enrichment_state(vgm_status, last_event_time DESC, container_no);
CREATE INDEX IF NOT EXISTS idx_container_state_history
    ON container_enrichment_state(history_status, last_event_time DESC, container_no);

-- Preserve the small legacy queue and its already-finished history state.
INSERT OR IGNORE INTO container_enrichment_state (
    container_no, source, first_seen_at, last_seen_at,
    vgm_status, history_status, history_attempt_count,
    history_last_attempt_at, history_last_error
)
SELECT UPPER(TRIM(q.container_no)), q.source, q.first_seen_at, q.first_seen_at,
       CASE WHEN EXISTS (
           SELECT 1 FROM fact_container_vgm v
           WHERE UPPER(TRIM(v.container_no))=UPPER(TRIM(q.container_no))
       ) THEN 'complete' ELSE 'pending' END,
       CASE WHEN q.status='complete' THEN 'complete' ELSE 'pending' END,
       q.attempt_count, q.last_attempt_at, q.last_error
FROM container_enrichment_queue q
WHERE q.container_no IS NOT NULL AND TRIM(q.container_no)<>'';

-- cargo-release exposes cargovolum as 件数 in the official UI, not volume.
-- Keep both legacy raw columns, and add explicit standardized fields.
ALTER TABLE fact_cargo_release ADD COLUMN piece_count REAL;
ALTER TABLE fact_cargo_release ADD COLUMN gross_weight_kg REAL;
ALTER TABLE fact_cargo_release ADD COLUMN gross_weight_unit TEXT;
ALTER TABLE fact_cargo_release ADD COLUMN weight_rule_version TEXT;

UPDATE fact_cargo_release
SET piece_count=cargo_volume
WHERE piece_count IS NULL AND cargo_volume IS NOT NULL;

-- Business rule cargo-weight-kg-v1:
-- * the official NPEDI UI uses kg for cargo-weight fields elsewhere;
-- * VGM is documented in kg;
-- * observed maxima (~318,014,000) are physically plausible only as kg for a
--   bulk-carrier bill, and impossible as tonnes.
UPDATE fact_cargo_release
SET gross_weight_kg=gross_weight,
    gross_weight_unit='kg',
    weight_rule_version='cargo-weight-kg-v1'
WHERE gross_weight IS NOT NULL AND gross_weight>=0;

ALTER TABLE agg_flow_daily ADD COLUMN released_weight_kg REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_flow_daily ADD COLUMN released_piece_count REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_flow_weekly ADD COLUMN released_weight_kg REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_flow_weekly ADD COLUMN released_piece_count REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_flow_daily_asof ADD COLUMN released_weight_kg REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_flow_daily_asof ADD COLUMN released_piece_count REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_flow_weekly_asof ADD COLUMN released_weight_kg REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_flow_weekly_asof ADD COLUMN released_piece_count REAL NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS agg_gate_daily (
    flow_date TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT '',
    in_gate_container_count INTEGER NOT NULL DEFAULT 0,
    out_gate_container_count INTEGER NOT NULL DEFAULT 0,
    unique_gate_container_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(flow_date,direction)
);
CREATE TABLE IF NOT EXISTS agg_gate_daily_asof (
    as_of_time TEXT NOT NULL,
    flow_date TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT '',
    in_gate_container_count INTEGER NOT NULL DEFAULT 0,
    out_gate_container_count INTEGER NOT NULL DEFAULT 0,
    unique_gate_container_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY(as_of_time,flow_date,direction)
);

-- Full local movement history without duplicating millions of CODECO rows.
-- `observed_at` is the collection time and therefore preserves as-of semantics:
-- a historical gate event is not visible to a backtest before it was fetched.
CREATE VIEW IF NOT EXISTS container_event_full AS
SELECT
    'api:' || e.business_key_hash AS event_id,
    e.container_no,
    e.event_type,
    e.event_code_raw,
    e.event_time,
    e.vessel_code,
    e.voyage,
    e.terminal_code,
    e.direction,
    e.bill_no,
    e.event_source,
    e.event_confidence,
    NULL AS observed_at
FROM fact_container_event e
UNION ALL
SELECT
    'gate-in:' || g.id AS event_id,
    UPPER(TRIM(g.ctnNo)) AS container_no,
    'IN_GATE' AS event_type,
    g.type AS event_code_raw,
    CASE
      WHEN length(TRIM(g.inGateTime))>=19 AND substr(TRIM(g.inGateTime),5,1)='-' THEN TRIM(g.inGateTime)
      WHEN length(TRIM(g.inGateTime))>=14 THEN
        substr(TRIM(g.inGateTime),1,4)||'-'||substr(TRIM(g.inGateTime),5,2)||'-'||substr(TRIM(g.inGateTime),7,2)||'T'||
        substr(TRIM(g.inGateTime),9,2)||':'||substr(TRIM(g.inGateTime),11,2)||':'||substr(TRIM(g.inGateTime),13,2)||'+00:00'
      ELSE NULL END AS event_time,
    g.vesselcode, g.voyage, NULL AS terminal_code, g.direct, g.blNo,
    'gate_events' AS event_source, 'observed' AS event_confidence,
    g.fetched_at AS observed_at
FROM gate_events g
WHERE g.ctnNo IS NOT NULL AND TRIM(g.ctnNo)<>''
  AND g.inGateTime IS NOT NULL AND TRIM(g.inGateTime)<>''
UNION ALL
SELECT
    'gate-out:' || g.id AS event_id,
    UPPER(TRIM(g.ctnNo)) AS container_no,
    'OUT_GATE' AS event_type,
    g.type AS event_code_raw,
    CASE
      WHEN length(TRIM(g.outGateTime))>=19 AND substr(TRIM(g.outGateTime),5,1)='-' THEN TRIM(g.outGateTime)
      WHEN length(TRIM(g.outGateTime))>=14 THEN
        substr(TRIM(g.outGateTime),1,4)||'-'||substr(TRIM(g.outGateTime),5,2)||'-'||substr(TRIM(g.outGateTime),7,2)||'T'||
        substr(TRIM(g.outGateTime),9,2)||':'||substr(TRIM(g.outGateTime),11,2)||':'||substr(TRIM(g.outGateTime),13,2)||'+00:00'
      ELSE NULL END AS event_time,
    g.vesselcode, g.voyage, NULL AS terminal_code, g.direct, g.blNo,
    'gate_events' AS event_source, 'observed' AS event_confidence,
    g.fetched_at AS observed_at
FROM gate_events g
WHERE g.ctnNo IS NOT NULL AND TRIM(g.ctnNo)<>''
  AND g.outGateTime IS NOT NULL AND TRIM(g.outGateTime)<>'';
