-- Cross-process leases let bounded workers consume the shrinking queue without
-- selecting the same containers. Abandoned leases are reclaimed by the code.
CREATE TABLE IF NOT EXISTS container_enrichment_claim (
    kind TEXT NOT NULL CHECK(kind IN ('vgm','history')),
    container_no TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (kind, container_no),
    FOREIGN KEY (container_no) REFERENCES container_enrichment_state(container_no)
);

CREATE INDEX IF NOT EXISTS idx_container_enrichment_claim_worker
    ON container_enrichment_claim(kind, worker_id);
