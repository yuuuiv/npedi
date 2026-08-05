CREATE INDEX IF NOT EXISTS idx_fact_plan_natural_snapshot
    ON fact_vessel_plan_snapshot(
        vessel_code, voyage, terminal_code, direction,
        snapshot_time DESC, vessel_plan_key DESC
    );
