CREATE INDEX IF NOT EXISTS idx_fact_record_version_current
    ON fact_record_version(fact_table, business_key_hash, record_hash);
