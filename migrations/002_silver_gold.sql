PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dim_vessel (
    vessel_key INTEGER PRIMARY KEY AUTOINCREMENT, vessel_code TEXT UNIQUE, vessel_en_name TEXT,
    vessel_cn_name TEXT, mmsi TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, raw_aliases TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dim_terminal (
    terminal_key INTEGER PRIMARY KEY AUTOINCREMENT, terminal_code TEXT NOT NULL UNIQUE,
    terminal_name TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dim_route (
    route_key INTEGER PRIMARY KEY AUTOINCREMENT, origin_port_code TEXT, destination_port_code TEXT,
    transshipment_port_code TEXT, direction TEXT, route_quality TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL, UNIQUE(origin_port_code,destination_port_code,transshipment_port_code,direction)
);
CREATE TABLE IF NOT EXISTS dim_cargo_group (
    cargo_group_key INTEGER PRIMARY KEY AUTOINCREMENT, level_1 TEXT, level_2 TEXT,
    normalized_name TEXT NOT NULL UNIQUE, rule_version TEXT NOT NULL, match_type TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_vessel_plan_snapshot (
    vessel_plan_key INTEGER PRIMARY KEY AUTOINCREMENT, business_key_hash TEXT NOT NULL, vessel_key INTEGER,
    vessel_code TEXT, vessel_en_name TEXT, vessel_cn_name TEXT, voyage TEXT, terminal_code TEXT, direction TEXT,
    trade_flag TEXT, ctn_start_time TEXT, ctn_end_time TEXT, custom_close_time TEXT, port_close_time TEXT,
    eta TEXT, etd TEXT, ata TEXT, atd TEXT, estimated_anchor_time TEXT, actual_anchor_time TEXT,
    last_port_code TEXT, next_port_code TEXT, berth_reference TEXT, status TEXT, published TEXT, publish_time TEXT,
    source_update_time TEXT, snapshot_time TEXT NOT NULL, record_hash TEXT NOT NULL, raw_json TEXT NOT NULL,
    UNIQUE(business_key_hash,snapshot_time)
);
CREATE INDEX IF NOT EXISTS idx_fact_plan_business ON fact_vessel_plan_snapshot(business_key_hash,snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_fact_plan_eta ON fact_vessel_plan_snapshot(eta);

CREATE TABLE IF NOT EXISTS fact_container_vgm (
    vgm_key INTEGER PRIMARY KEY AUTOINCREMENT, business_key_hash TEXT NOT NULL UNIQUE, container_no TEXT,
    vessel_code TEXT, vessel_name_raw TEXT, voyage TEXT, terminal_code TEXT, operator_code TEXT, direction TEXT,
    container_type TEXT, vgm_weight_kg REAL, vgm_method TEXT, operator_time TEXT, terminal_received_time TEXT,
    result_code TEXT, result_description TEXT, sender_code TEXT, receiver_code TEXT, ingested_at TEXT NOT NULL,
    record_hash TEXT NOT NULL, raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_vgm_event ON fact_container_vgm(operator_time);

CREATE TABLE IF NOT EXISTS fact_cargo_release (
    release_key INTEGER PRIMARY KEY AUTOINCREMENT, business_key_hash TEXT NOT NULL UNIQUE,
    vessel_code TEXT, vessel_name_raw TEXT, voyage TEXT, direction TEXT, bill_no TEXT, pass_time TEXT,
    terminal_code TEXT, flag TEXT, cargo_volume REAL, gross_weight REAL, ingested_at TEXT NOT NULL,
    record_hash TEXT NOT NULL, raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_release_event ON fact_cargo_release(pass_time);

CREATE TABLE IF NOT EXISTS fact_transshipment (
    transshipment_key INTEGER PRIMARY KEY AUTOINCREMENT, business_key_hash TEXT NOT NULL UNIQUE,
    container_no TEXT, operator_code TEXT, bill_no TEXT, quantity REAL, weight REAL, volume REAL,
    cargo_description_raw TEXT, cargo_group_key INTEGER, cargo_group_name TEXT, cargo_match_type TEXT, first_vessel_code TEXT, first_voyage TEXT,
    first_load_port_code TEXT, first_discharge_port_code TEXT, second_vessel_code TEXT, second_voyage TEXT,
    second_trans_port_code TEXT, sailing_date TEXT, cutoff_date TEXT, terminal_code TEXT,
    check_flag TEXT, send_flag TEXT, ingested_at TEXT NOT NULL, record_hash TEXT NOT NULL, raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_trans_event ON fact_transshipment(sailing_date);

CREATE TABLE IF NOT EXISTS fact_container_event (
    event_key INTEGER PRIMARY KEY AUTOINCREMENT, business_key_hash TEXT NOT NULL UNIQUE, container_no TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('UNKNOWN')), event_code_raw TEXT, event_time TEXT,
    vessel_code TEXT, voyage TEXT, terminal_code TEXT, direction TEXT, bill_no TEXT, event_source TEXT NOT NULL,
    event_confidence TEXT NOT NULL DEFAULT 'unverified', source_record_hash TEXT NOT NULL, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS container_enrichment_queue (
    container_no TEXT PRIMARY KEY, priority INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, last_attempt_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', last_error TEXT
);

CREATE TABLE IF NOT EXISTS agg_flow_daily (
    flow_date TEXT NOT NULL, terminal_code TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '', route_key TEXT NOT NULL DEFAULT '',
    cargo_group_key TEXT NOT NULL DEFAULT '', vessel_operator TEXT NOT NULL DEFAULT '',
    vgm_container_count INTEGER NOT NULL DEFAULT 0, vgm_weight_kg REAL NOT NULL DEFAULT 0,
    released_bill_count INTEGER NOT NULL DEFAULT 0, released_weight REAL NOT NULL DEFAULT 0, released_volume REAL NOT NULL DEFAULT 0,
    transshipment_container_count INTEGER NOT NULL DEFAULT 0, transshipment_weight REAL NOT NULL DEFAULT 0, transshipment_volume REAL NOT NULL DEFAULT 0,
    planned_vessel_call_count INTEGER NOT NULL DEFAULT 0, actual_vessel_call_count INTEGER NOT NULL DEFAULT 0,
    avg_arrival_delay_hours REAL, avg_departure_delay_hours REAL, created_at TEXT NOT NULL,
    PRIMARY KEY(flow_date,terminal_code,direction,route_key,cargo_group_key,vessel_operator)
);
CREATE TABLE IF NOT EXISTS agg_flow_weekly (
    week_start TEXT NOT NULL, terminal_code TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT '', route_key TEXT NOT NULL DEFAULT '',
    cargo_group_key TEXT NOT NULL DEFAULT '', vessel_operator TEXT NOT NULL DEFAULT '',
    vgm_container_count INTEGER NOT NULL DEFAULT 0, vgm_weight_kg REAL NOT NULL DEFAULT 0,
    released_bill_count INTEGER NOT NULL DEFAULT 0, released_weight REAL NOT NULL DEFAULT 0, released_volume REAL NOT NULL DEFAULT 0,
    transshipment_container_count INTEGER NOT NULL DEFAULT 0, transshipment_weight REAL NOT NULL DEFAULT 0, transshipment_volume REAL NOT NULL DEFAULT 0,
    planned_vessel_call_count INTEGER NOT NULL DEFAULT 0, actual_vessel_call_count INTEGER NOT NULL DEFAULT 0,
    avg_arrival_delay_hours REAL, avg_departure_delay_hours REAL, created_at TEXT NOT NULL,
    PRIMARY KEY(week_start,terminal_code,direction,route_key,cargo_group_key,vessel_operator)
);
CREATE TABLE IF NOT EXISTS mart_curve_series (
    curve_id TEXT NOT NULL, curve_type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_key TEXT NOT NULL,
    granularity TEXT NOT NULL CHECK(granularity IN ('day','week','month')), time_bucket TEXT NOT NULL,
    value REAL, lower_bound REAL, upper_bound REAL, quality_flag TEXT NOT NULL DEFAULT 'complete',
    model_version TEXT NOT NULL, computed_at TEXT NOT NULL, source_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(curve_id,time_bucket,model_version)
);
CREATE TABLE IF NOT EXISTS feature_series_window (
    feature_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_key TEXT NOT NULL,
    window_start TEXT NOT NULL, window_end TEXT NOT NULL, frequency TEXT NOT NULL, feature_version TEXT NOT NULL,
    mean REAL, median REAL, std REAL, coefficient_of_variation REAL, trend_slope REAL, recent_growth_rate REAL, growth_4w REAL, growth_13w REAL,
    peak_to_median REAL, zero_ratio REAL, seasonal_strength REAL, autocorrelation_lag_1 REAL, autocorrelation_lag_7_or_4 REAL,
    arrival_delay_mean REAL, arrival_delay_p90 REAL, transshipment_share REAL, plan_revision_frequency REAL, data_completeness REAL,
    feature_json TEXT NOT NULL DEFAULT '{}', UNIQUE(entity_type,entity_key,window_start,window_end,feature_version)
);
CREATE TABLE IF NOT EXISTS cluster_run (
    cluster_run_id TEXT PRIMARY KEY, algorithm TEXT NOT NULL, algorithm_version TEXT NOT NULL,
    feature_version TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL, entity_type TEXT NOT NULL,
    parameters_json TEXT NOT NULL DEFAULT '{}', normalization_method TEXT NOT NULL, sample_count INTEGER NOT NULL,
    cluster_count INTEGER NOT NULL, silhouette_score REAL, stability_score REAL, created_at TEXT NOT NULL, artifact_path TEXT
);
CREATE TABLE IF NOT EXISTS cluster_assignment (
    cluster_run_id TEXT NOT NULL REFERENCES cluster_run(cluster_run_id), entity_key TEXT NOT NULL,
    cluster_id INTEGER, membership_probability REAL, distance_to_prototype REAL, is_outlier INTEGER NOT NULL DEFAULT 0,
    business_label TEXT, assigned_at TEXT NOT NULL, PRIMARY KEY(cluster_run_id,entity_key)
);
CREATE TABLE IF NOT EXISTS change_point_event (
    change_point_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_key TEXT NOT NULL, curve_type TEXT NOT NULL,
    change_time TEXT NOT NULL, change_score REAL, before_level REAL, after_level REAL, before_trend REAL, after_trend REAL,
    algorithm TEXT NOT NULL, model_version TEXT NOT NULL, detected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anomaly_event (
    anomaly_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_key TEXT NOT NULL, curve_type TEXT NOT NULL,
    time_bucket TEXT NOT NULL, anomaly_score REAL, method TEXT NOT NULL, model_version TEXT NOT NULL,
    detected_at TEXT NOT NULL, raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS trend_snapshot (
    trend_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_key TEXT NOT NULL, curve_type TEXT NOT NULL, as_of_time TEXT NOT NULL,
    trend_state TEXT NOT NULL, growth_1w REAL, growth_4w REAL, growth_13w REAL, volatility_13w REAL,
    current_cluster INTEGER, previous_cluster INTEGER, change_point_recent INTEGER NOT NULL DEFAULT 0,
    anomaly_score REAL, data_quality REAL, explanation_json TEXT NOT NULL DEFAULT '{}', UNIQUE(entity_key,curve_type,as_of_time)
);



