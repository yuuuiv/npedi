PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS crawl_run (
    id TEXT PRIMARY KEY, job_name TEXT NOT NULL, endpoint_name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('backfill','incremental','rescan','enrichment','snapshot','upsert')),
    started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running','success','partial','failed','interrupted')),
    request_count INTEGER NOT NULL DEFAULT 0, row_count_raw INTEGER NOT NULL DEFAULT 0,
    row_count_inserted INTEGER NOT NULL DEFAULT 0, row_count_updated INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0, config_json TEXT NOT NULL DEFAULT '{}', error_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_crawl_run_job_started ON crawl_run(job_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_crawl_run_endpoint_status ON crawl_run(endpoint_name, status, started_at DESC);

CREATE TABLE IF NOT EXISTS crawl_checkpoint (
    job_name TEXT NOT NULL, partition_key TEXT NOT NULL, next_page INTEGER NOT NULL DEFAULT 1,
    observed_total INTEGER, last_success_at TEXT, cursor_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
    PRIMARY KEY (job_name, partition_key)
);

CREATE TABLE IF NOT EXISTS raw_api_response (
    id INTEGER PRIMARY KEY AUTOINCREMENT, crawl_run_id TEXT NOT NULL REFERENCES crawl_run(id),
    endpoint_name TEXT NOT NULL, request_fingerprint TEXT NOT NULL, page_num INTEGER NOT NULL,
    business_partition TEXT NOT NULL, response_code INTEGER, response_msg TEXT,
    http_status INTEGER NOT NULL DEFAULT 200, payload_hash TEXT NOT NULL, raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
    UNIQUE(endpoint_name, request_fingerprint, page_num, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_raw_api_response_run ON raw_api_response(crawl_run_id);
CREATE INDEX IF NOT EXISTS idx_raw_api_response_endpoint ON raw_api_response(endpoint_name, fetched_at DESC);

CREATE TABLE IF NOT EXISTS ingest_error (
    id INTEGER PRIMARY KEY AUTOINCREMENT, crawl_run_id TEXT REFERENCES crawl_run(id), endpoint_name TEXT NOT NULL,
    stage TEXT NOT NULL, message TEXT NOT NULL, request_json TEXT NOT NULL DEFAULT '{}',
    row_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze_record (
    endpoint_name TEXT NOT NULL, business_key_hash TEXT NOT NULL, record_hash TEXT NOT NULL,
    event_time TEXT, source_update_time TEXT, ingested_at TEXT NOT NULL, raw_json TEXT NOT NULL,
    PRIMARY KEY (endpoint_name, business_key_hash)
);
CREATE INDEX IF NOT EXISTS idx_bronze_record_event_time ON bronze_record(endpoint_name, event_time);

CREATE TABLE IF NOT EXISTS silver_vessel_plan (
    business_key_hash TEXT NOT NULL, vessel_code TEXT, vessel_en_name TEXT, vessel_cn_name TEXT,
    voyage TEXT, terminal_code TEXT, direction TEXT, trade_flag TEXT, ctn_start_time TEXT, ctn_end_time TEXT,
    custom_close_time TEXT, port_close_time TEXT, eta TEXT, etd TEXT, ata TEXT, atd TEXT,
    estimated_anchor_time TEXT, actual_anchor_time TEXT, last_port_code TEXT, next_port_code TEXT,
    berth_reference TEXT, status TEXT, published TEXT, publish_time TEXT, source_update_time TEXT,
    snapshot_time TEXT NOT NULL, record_hash TEXT NOT NULL, quality_json TEXT NOT NULL DEFAULT '{}', raw_json TEXT NOT NULL,
    PRIMARY KEY (business_key_hash, snapshot_time)
);
CREATE INDEX IF NOT EXISTS idx_silver_plan_eta ON silver_vessel_plan(eta);

CREATE TABLE IF NOT EXISTS silver_container_notice (
    business_key_hash TEXT NOT NULL, vessel_code TEXT, vessel_en_name TEXT, voyage TEXT, terminal_code TEXT,
    direction TEXT, ctn_start_time TEXT, ctn_end_time TEXT, ports_raw TEXT, vessel_operator TEXT,
    source_update_time TEXT, ingested_at TEXT NOT NULL, record_hash TEXT NOT NULL,
    quality_json TEXT NOT NULL DEFAULT '{}', raw_json TEXT NOT NULL,
    PRIMARY KEY (business_key_hash, ingested_at)
);

CREATE TABLE IF NOT EXISTS schema_observation (
    endpoint_name TEXT NOT NULL, field_name TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL, sample_type TEXT, PRIMARY KEY (endpoint_name, field_name)
);

