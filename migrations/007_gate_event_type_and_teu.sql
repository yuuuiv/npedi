PRAGMA foreign_keys = ON;

ALTER TABLE agg_gate_daily ADD COLUMN in_gate_teu REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_gate_daily ADD COLUMN out_gate_teu REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_gate_daily ADD COLUMN in_teu_known_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agg_gate_daily ADD COLUMN out_teu_known_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agg_gate_daily_asof ADD COLUMN in_gate_teu REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_gate_daily_asof ADD COLUMN out_gate_teu REAL NOT NULL DEFAULT 0;
ALTER TABLE agg_gate_daily_asof ADD COLUMN in_teu_known_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agg_gate_daily_asof ADD COLUMN out_teu_known_count INTEGER NOT NULL DEFAULT 0;

-- A GATE_OUT payload can carry the box's earlier inGateTime. Event type, not
-- merely a non-null timestamp, determines whether the row is an in/out event.
DROP VIEW IF EXISTS container_event_full;
CREATE VIEW container_event_full AS
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
    'gate-in:' || g.id,
    UPPER(TRIM(g.ctnNo)),
    'IN_GATE', g.type,
    CASE
      WHEN length(TRIM(g.inGateTime))>=19 AND substr(TRIM(g.inGateTime),5,1)='-' THEN TRIM(g.inGateTime)
      WHEN length(TRIM(g.inGateTime))>=14 THEN
        substr(TRIM(g.inGateTime),1,4)||'-'||substr(TRIM(g.inGateTime),5,2)||'-'||substr(TRIM(g.inGateTime),7,2)||'T'||
        substr(TRIM(g.inGateTime),9,2)||':'||substr(TRIM(g.inGateTime),11,2)||':'||substr(TRIM(g.inGateTime),13,2)||'+00:00'
      ELSE NULL END,
    g.vesselcode, g.voyage, NULL, g.direct, g.blNo,
    'gate_events', 'observed', g.fetched_at
FROM gate_events g
WHERE g.type='GATE_IN'
  AND g.ctnNo IS NOT NULL AND TRIM(g.ctnNo)<>''
  AND g.inGateTime IS NOT NULL AND TRIM(g.inGateTime)<>''
UNION ALL
SELECT
    'gate-out:' || g.id,
    UPPER(TRIM(g.ctnNo)),
    'OUT_GATE', g.type,
    CASE
      WHEN length(TRIM(g.outGateTime))>=19 AND substr(TRIM(g.outGateTime),5,1)='-' THEN TRIM(g.outGateTime)
      WHEN length(TRIM(g.outGateTime))>=14 THEN
        substr(TRIM(g.outGateTime),1,4)||'-'||substr(TRIM(g.outGateTime),5,2)||'-'||substr(TRIM(g.outGateTime),7,2)||'T'||
        substr(TRIM(g.outGateTime),9,2)||':'||substr(TRIM(g.outGateTime),11,2)||':'||substr(TRIM(g.outGateTime),13,2)||'+00:00'
      ELSE NULL END,
    g.vesselcode, g.voyage, NULL, g.direct, g.blNo,
    'gate_events', 'observed', g.fetched_at
FROM gate_events g
WHERE g.type='GATE_OUT'
  AND g.ctnNo IS NOT NULL AND TRIM(g.ctnNo)<>''
  AND g.outGateTime IS NOT NULL AND TRIM(g.outGateTime)<>'';
