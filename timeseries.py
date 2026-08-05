"""Bronze/Phase-1 crawler framework for the documented timeseries endpoints."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from client import ApiError, AuthExpired, NpediClient
from config import Config

ROOT = Path(__file__).resolve().parent
VERSIONED_FACT_TABLES = frozenset({
    "fact_container_vgm",
    "fact_cargo_release",
    "fact_transshipment",
    "fact_container_event",
})


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_time(value: Any) -> str | None:
    text = clean(value)
    if not text:
        return None
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def parse_number(value: Any) -> float | None:
    text = clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_name(value: Any) -> str | None:
    text = clean(value)
    return " ".join(text.replace("　", " ").upper().split()) if text else None


@dataclass(frozen=True)
class RequestSpec:
    partition_key: str
    filters: dict[str, str]
    page_size: int


class TimeseriesStore:
    def __init__(self, path: Path, migration_dir: Path = ROOT / "migrations"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=60.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.apply_migrations(migration_dir)
        self._bootstrap_fact_versions()

    def apply_migrations(self, migration_dir: Path) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migration (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        for path in sorted(Path(migration_dir).glob("*.sql")):
            if self.conn.execute("SELECT 1 FROM schema_migration WHERE name=?", (path.name,)).fetchone():
                continue
            self.conn.executescript(path.read_text(encoding="utf-8"))
            self.conn.execute("INSERT INTO schema_migration(name, applied_at) VALUES(?,?)", (path.name, now_utc()))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _bootstrap_fact_versions(self) -> None:
        """Seed version history for facts written before migration 003."""
        marker = "__fact_versions_bootstrapped_v1__"
        if self.conn.execute("SELECT 1 FROM schema_migration WHERE name=?", (marker,)).fetchone():
            return
        for table in VERSIONED_FACT_TABLES:
            hash_field = "source_record_hash" if table == "fact_container_event" else "record_hash"
            # Do not load or reprocess the entire current projection on every
            # CLI startup. Only legacy rows without a matching version need
            # seeding; normal upserts append their own versions.
            rows = self.conn.execute(
                f"""SELECT f.* FROM {table} AS f
                    WHERE NOT EXISTS (
                        SELECT 1 FROM fact_record_version AS v
                        WHERE v.fact_table=?
                          AND v.business_key_hash=f.business_key_hash
                          AND v.record_hash=f.{hash_field}
                    )""",
                (table,),
            )
            for row in rows:
                record_hash = row[hash_field]
                observed_at = row["ingested_at"] if "ingested_at" in row.keys() else now_utc()
                normalized = {key: row[key] for key in row.keys() if key not in {"raw_json"}}
                event_time = next((row[key] for key in ("operator_time", "pass_time", "sailing_date", "event_time") if key in row.keys()), None)
                self.conn.execute("""INSERT OR IGNORE INTO fact_record_version
                    (fact_table,business_key_hash,observed_at,event_time,record_hash,normalized_json,raw_json)
                    VALUES(?,?,?,?,?,?,?)""", (table, row["business_key_hash"], observed_at, event_time, record_hash, stable(normalized), row["raw_json"] or ""))
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migration(name, applied_at) VALUES(?,?)",
            (marker, now_utc()),
        )
        self.conn.commit()

    def __enter__(self) -> "TimeseriesStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def start_run(self, job: str, endpoint: str, mode: str, config: dict[str, Any]) -> str:
        run_id = str(uuid.uuid4())
        self.conn.execute("UPDATE crawl_run SET status='interrupted', finished_at=?, error_summary=? WHERE status='running'", (now_utc(), "superseded by later run"))
        self.conn.execute("INSERT INTO crawl_run(id,job_name,endpoint_name,mode,started_at,status,config_json) VALUES(?,?,?,?,?,?,?)", (run_id, job, endpoint, mode, now_utc(), "running", stable(config)))
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, stats: dict[str, int], error: str | None = None) -> None:
        self.conn.execute("""UPDATE crawl_run SET finished_at=?,status=?,request_count=?,row_count_raw=?,row_count_inserted=?,row_count_updated=?,error_count=?,error_summary=? WHERE id=?""", (now_utc(), status, stats["requests"], stats["raw"], stats["inserted"], stats["updated"], stats["errors"], error, run_id))
        self.conn.commit()

    def checkpoint(self, job: str, partition: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM crawl_checkpoint WHERE job_name=? AND partition_key=?", (job, partition)).fetchone()

    def save_checkpoint(self, job: str, partition: str, next_page: int, total: int | None) -> None:
        ts = now_utc()
        self.conn.execute("""INSERT INTO crawl_checkpoint(job_name,partition_key,next_page,observed_total,last_success_at,cursor_json,updated_at) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(job_name,partition_key) DO UPDATE SET next_page=excluded.next_page,observed_total=excluded.observed_total,last_success_at=excluded.last_success_at,cursor_json=excluded.cursor_json,updated_at=excluded.updated_at""", (job, partition, next_page, total, ts, "{}", ts))
        self.conn.commit()

    def raw_page(self, run_id: str, endpoint: str, request: RequestSpec, page: int, response: dict[str, Any]) -> bool:
        raw = stable(response)
        cur = self.conn.execute("""INSERT OR IGNORE INTO raw_api_response(crawl_run_id,endpoint_name,request_fingerprint,page_num,business_partition,response_code,response_msg,http_status,payload_hash,raw_json,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (run_id, endpoint, digest(stable({"endpoint": endpoint, "filters": request.filters})), page, request.partition_key, response.get("code"), response.get("msg"), 200, digest(raw), raw, now_utc()))
        self.conn.commit()
        return cur.rowcount == 1

    def error(self, run_id: str, endpoint: str, stage: str, message: str, request: dict[str, Any] | None = None, row: dict[str, Any] | None = None) -> None:
        self.conn.execute("INSERT INTO ingest_error(crawl_run_id,endpoint_name,stage,message,request_json,row_json,created_at) VALUES(?,?,?,?,?,?,?)", (run_id, endpoint, stage, message, stable(request or {}), stable(row or {}), now_utc()))
        self.conn.commit()

    def observe_schema(self, endpoint: str, row: dict[str, Any]) -> None:
        ts = now_utc()
        for key, value in row.items():
            self.conn.execute("""INSERT INTO schema_observation(endpoint_name,field_name,first_seen_at,last_seen_at,sample_type) VALUES(?,?,?,?,?)
            ON CONFLICT(endpoint_name,field_name) DO UPDATE SET last_seen_at=excluded.last_seen_at""", (endpoint, key, ts, ts, type(value).__name__))
        self.conn.commit()

    def upsert_bronze(self, endpoint: str, key: str, row: dict[str, Any], event_time: str | None, source_update_time: str | None) -> tuple[bool, bool]:
        raw = stable(row)
        record_hash = digest(raw)
        key_hash = digest(key)
        old = self.conn.execute("SELECT record_hash FROM bronze_record WHERE endpoint_name=? AND business_key_hash=?", (endpoint, key_hash)).fetchone()
        if old and old[0] == record_hash:
            return False, False
        self.conn.execute("""INSERT INTO bronze_record(endpoint_name,business_key_hash,record_hash,event_time,source_update_time,ingested_at,raw_json) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(endpoint_name,business_key_hash) DO UPDATE SET record_hash=excluded.record_hash,event_time=excluded.event_time,source_update_time=excluded.source_update_time,ingested_at=excluded.ingested_at,raw_json=excluded.raw_json""", (endpoint, key_hash, record_hash, event_time, source_update_time, now_utc(), raw))
        self.conn.commit()
        return old is None, old is not None

    def insert_plan(self, row: dict[str, Any]) -> tuple[bool, bool]:
        fields = ("business_key_hash","vessel_code","vessel_en_name","vessel_cn_name","voyage","terminal_code","direction","trade_flag","ctn_start_time","ctn_end_time","custom_close_time","port_close_time","eta","etd","ata","atd","estimated_anchor_time","actual_anchor_time","last_port_code","next_port_code","berth_reference","status","published","publish_time","source_update_time","snapshot_time","record_hash","quality_json","raw_json")
        old = self.conn.execute("SELECT 1 FROM silver_vessel_plan WHERE business_key_hash=? AND snapshot_time=?", (row["business_key_hash"], row["snapshot_time"])).fetchone()
        sql = "INSERT OR REPLACE INTO silver_vessel_plan(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")"
        self.conn.execute(sql, tuple(row.get(field) for field in fields))
        self.conn.commit()
        return old is None, old is not None

    def upsert_dimensions(self, row: dict[str, Any]) -> None:
        ts = now_utc()
        vessel_code, terminal_code = row.get("vessel_code"), row.get("terminal_code")
        if vessel_code:
            self.conn.execute("""INSERT INTO dim_vessel(vessel_code,vessel_en_name,vessel_cn_name,first_seen_at,last_seen_at,raw_aliases) VALUES(?,?,?,?,?,?)
            ON CONFLICT(vessel_code) DO UPDATE SET vessel_en_name=COALESCE(excluded.vessel_en_name,dim_vessel.vessel_en_name), vessel_cn_name=COALESCE(excluded.vessel_cn_name,dim_vessel.vessel_cn_name), last_seen_at=excluded.last_seen_at""", (vessel_code, row.get("vessel_en_name"), row.get("vessel_cn_name"), ts, ts, row.get("raw_json", "{}")))
        if terminal_code:
            self.conn.execute("""INSERT INTO dim_terminal(terminal_code,terminal_name,first_seen_at,last_seen_at,raw_json) VALUES(?,?,?,?,?)
            ON CONFLICT(terminal_code) DO UPDATE SET last_seen_at=excluded.last_seen_at""", (terminal_code, None, ts, ts, row.get("raw_json", "{}")))
        self.conn.commit()
    def upsert_fact(self, table: str, row: dict[str, Any], fields: tuple[str, ...]) -> tuple[bool, bool]:
        allowed = {"fact_vessel_plan_snapshot", "fact_container_vgm", "fact_cargo_release", "fact_transshipment", "fact_container_event"}
        if table not in allowed:
            raise ValueError(f"unsupported fact table: {table}")
        hash_field = "source_record_hash" if table == "fact_container_event" else "record_hash"
        old = self.conn.execute(f"SELECT {hash_field} FROM {table} WHERE business_key_hash=?", (row["business_key_hash"],)).fetchone()
        if old and old[0] == row.get(hash_field):
            return False, False
        if table in VERSIONED_FACT_TABLES:
            observed_at = clean(row.get("ingested_at")) or now_utc()
            event_time = next((clean(row.get(key)) for key in ("operator_time", "pass_time", "sailing_date", "event_time") if row.get(key)), None)
            record_hash = row.get(hash_field) or digest(stable(row))
            normalized = {field: row.get(field) for field in fields if field != "raw_json"}
            self.conn.execute("""INSERT OR IGNORE INTO fact_record_version
                (fact_table,business_key_hash,observed_at,event_time,record_hash,normalized_json,raw_json)
                VALUES(?,?,?,?,?,?,?)""", (table, row["business_key_hash"], observed_at, event_time, record_hash, stable(normalized), row.get("raw_json") or ""))
        sql = "INSERT OR REPLACE INTO " + table + "(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")"
        self.conn.execute(sql, tuple(row.get(field) for field in fields))
        self.conn.commit()
        return old is None, old is not None
    def insert_notice(self, row: dict[str, Any]) -> tuple[bool, bool]:
        fields = ("business_key_hash","vessel_code","vessel_en_name","voyage","terminal_code","direction","ctn_start_time","ctn_end_time","ports_raw","vessel_operator","source_update_time","ingested_at","record_hash","quality_json","raw_json")
        old = self.conn.execute("SELECT 1 FROM silver_container_notice WHERE business_key_hash=? AND ingested_at=?", (row["business_key_hash"], row["ingested_at"])).fetchone()
        sql = "INSERT OR REPLACE INTO silver_container_notice(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")"
        self.conn.execute(sql, tuple(row.get(field) for field in fields))
        self.conn.commit()
        return old is None, old is not None


class BaseCrawler:
    endpoint_name = ""
    mode = "snapshot"

    def __init__(self, client: Any, store: TimeseriesStore, config: Config):
        self.client, self.store, self.config = client, store, config

    def build_requests(self, context: dict[str, Any]) -> Iterable[RequestSpec]:
        yield RequestSpec("default", {key: str(value) for key, value in context.items()}, self.config.page_size)

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        raise NotImplementedError

    def validate_response(self, request: RequestSpec, response: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
        """Return a safety error when a response cannot be trusted for a request.

        Crawlers may override this for endpoints whose server-side filters are
        known to be unreliable.  Returning an error stops the whole crawl
        before any returned rows are persisted or the next partition is
        requested.
        """
        return None

    def extract_rows(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        data = response.get("data") or {}
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("list", [])
        else:
            rows = []
        return [row for row in rows if isinstance(row, dict)]

    def get_total(self, response: dict[str, Any]) -> int | None:
        data = response.get("data") or {}
        if not isinstance(data, dict) or data.get("total") is None:
            return None
        try:
            return int(data["total"])
        except (TypeError, ValueError):
            return None

    def business_key(self, row: dict[str, Any]) -> str:
        return stable(row)

    def event_time(self, row: dict[str, Any]) -> str | None:
        return None

    def source_update_time(self, row: dict[str, Any]) -> str | None:
        return None

    def normalize(self, row: dict[str, Any]) -> dict[str, Any]:
        return row

    def persist(self, row: dict[str, Any]) -> tuple[bool, bool]:
        normalized = self.normalize(row)
        self.store.upsert_dimensions(normalized)
        return self.store.upsert_bronze(self.endpoint_name, self.business_key(row), normalized, self.event_time(row), self.source_update_time(row))

    def on_request_success(self, request: RequestSpec) -> None:
        """Run after one request partition has completed without errors."""

    def on_request_failure(self, request: RequestSpec) -> None:
        """Run after one request partition has stopped with an error."""

    def crawl(self, context: dict[str, Any] | None = None, *, resume: bool = False) -> dict[str, Any]:
        context = context or {}
        run_id = self.store.start_run(self.endpoint_name, self.endpoint_name, self.mode, context)
        stats = {"requests": 0, "raw": 0, "seen": 0, "inserted": 0, "updated": 0, "errors": 0}
        abort_reason: str | None = None
        try:
            for request in self.build_requests(context):
                errors_before_request = stats["errors"]
                request_complete = False
                checkpoint = self.store.checkpoint(self.endpoint_name, request.partition_key)
                page = int(checkpoint["next_page"]) if resume and checkpoint else 1
                if (
                    resume
                    and checkpoint
                    and checkpoint["observed_total"] is not None
                    and (page - 1) * request.page_size >= int(checkpoint["observed_total"])
                ):
                    # This partition reached its reported total in an earlier
                    # process. Treat it as complete without making a redundant
                    # post-reboot request. The success hook also upgrades old
                    # container-history queue rows to status=complete.
                    self.on_request_success(request)
                    continue
                page_hashes: list[str] = []
                while page <= self.config.max_pages_per_query:
                    try:
                        response = self.fetch_page(request, page)
                    except ApiError as exc:
                        stats["errors"] += 1
                        self.store.error(run_id, self.endpoint_name, "request", str(exc), {"partition": request.partition_key, "page": page, "filters": request.filters})
                        break
                    stats["requests"] += 1
                    stats["raw"] += int(self.store.raw_page(run_id, self.endpoint_name, request, page, response))
                    rows = self.extract_rows(response)
                    validation_error = self.validate_response(request, response, rows)
                    if validation_error:
                        abort_reason = validation_error
                        stats["errors"] += 1
                        self.store.error(
                            run_id,
                            self.endpoint_name,
                            "response_validation",
                            validation_error,
                            {"partition": request.partition_key, "page": page, "filters": request.filters},
                        )
                        break
                    total = self.get_total(response)
                    page_hash = digest(stable(rows))
                    if page_hash in page_hashes[-2:]:
                        self.store.error(run_id, self.endpoint_name, "pagination", f"repeated page hash at page {page}", {"page": page})
                        stats["errors"] += 1
                        break
                    page_hashes.append(page_hash)
                    for row in rows:
                        stats["seen"] += 1
                        self.store.observe_schema(self.endpoint_name, row)
                        try:
                            new, updated = self.persist(row)
                        except (KeyError, TypeError, ValueError) as exc:
                            self.store.error(run_id, self.endpoint_name, "normalize", str(exc), {"page": page}, row)
                            stats["errors"] += 1
                            continue
                        stats["inserted"] += int(new)
                        stats["updated"] += int(updated)
                    self.store.save_checkpoint(self.endpoint_name, request.partition_key, page + 1, total)
                    if not rows or (total is not None and page * request.page_size >= total) or len(rows) < request.page_size:
                        request_complete = True
                        break
                    page += 1
                else:
                    stats["errors"] += 1
                    self.store.error(run_id, self.endpoint_name, "pagination", "max page limit reached")
                if request_complete and stats["errors"] == errors_before_request:
                    self.on_request_success(request)
                else:
                    self.on_request_failure(request)
                if abort_reason:
                    break
            status = "partial" if stats["errors"] else "success"
            self.store.finish_run(run_id, status, stats, abort_reason)
            return {"run_id": run_id, "status": status, **stats}
        except AuthExpired:
            stats["errors"] += 1
            self.store.finish_run(run_id, "failed", stats, "authentication expired")
            raise
        except Exception as exc:
            stats["errors"] += 1
            self.store.error(run_id, self.endpoint_name, "crawl", str(exc))
            self.store.finish_run(run_id, "failed", stats, str(exc))
            raise


class VesselPlanCrawler(BaseCrawler):
    endpoint_name = "vessel_plan"
    mode = "snapshot"

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        return self.client.vessel_plan_page(page, page_size=request.page_size, **request.filters)

    def business_key(self, row: dict[str, Any]) -> str:
        # ETA is mutable plan content, not identity. Keeping it out of the key
        # lets as-of reconstruction see a revised plan instead of two voyages.
        parts = [clean(row.get(key)) or "" for key in ("vesselUnCode", "voyage", "terminal", "vesselDirect")]
        return "|".join(parts) if all(parts) else stable(row)

    def event_time(self, row: dict[str, Any]) -> str | None:
        return parse_time(row.get("eta"))

    def source_update_time(self, row: dict[str, Any]) -> str | None:
        return parse_time(row.get("publishTime"))

    def normalize(self, row: dict[str, Any]) -> dict[str, Any]:
        sources = {"ctn_start_time":"ctnStartTime","ctn_end_time":"ctnEndTime","custom_close_time":"customCloseTime","port_close_time":"portCloseTime","eta":"eta","etd":"etd","ata":"ata","atd":"atd","estimated_anchor_time":"etanchor","actual_anchor_time":"atanchor","publish_time":"publishTime"}
        names = {"vessel_code":"vesselUnCode","vessel_en_name":"vesselEnName","vessel_cn_name":"vesselCnName","voyage":"voyage","terminal_code":"terminal","direction":"vesselDirect","trade_flag":"tradeFlag","last_port_code":"lastPortCode","next_port_code":"nextPortCode","berth_reference":"berthReference","status":"status","published":"published"}
        normalized = {key: parse_time(row.get(source)) for key, source in sources.items()}
        normalized.update({key: clean(row.get(source)) for key, source in names.items()})
        normalized["vessel_en_name"] = normalize_name(row.get("vesselEnName"))
        normalized.update({"business_key_hash": digest(self.business_key(row)), "source_update_time": self.source_update_time(row), "snapshot_time": now_utc(), "record_hash": digest(stable(row)), "quality_json": stable({}), "raw_json": stable(row)})
        return normalized

    def persist(self, row: dict[str, Any]) -> tuple[bool, bool]:
        normalized = self.normalize(row)
        self.store.upsert_dimensions(normalized)
        self.store.upsert_bronze(self.endpoint_name, self.business_key(row), normalized, normalized["eta"], normalized["source_update_time"])
        self.store.upsert_fact("fact_vessel_plan_snapshot", normalized, ("business_key_hash","vessel_code","vessel_en_name","vessel_cn_name","voyage","terminal_code","direction","trade_flag","ctn_start_time","ctn_end_time","custom_close_time","port_close_time","eta","etd","ata","atd","estimated_anchor_time","actual_anchor_time","last_port_code","next_port_code","berth_reference","status","published","publish_time","source_update_time","snapshot_time","record_hash","raw_json"))
        return self.store.insert_plan(normalized)


class ContainerNoticeCrawler(BaseCrawler):
    endpoint_name = "container_notice"
    mode = "snapshot"

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        return self.client.container_notice_page(page, page_size=request.page_size, **request.filters)

    def business_key(self, row: dict[str, Any]) -> str:
        parts = [clean(row.get(key)) or "" for key in ("vesselcode", "voyage", "matou", "ctnstart", "ctnend")]
        return "|".join(parts) if all(parts) else stable(row)

    def event_time(self, row: dict[str, Any]) -> str | None:
        return parse_time(row.get("ctnstart"))

    def source_update_time(self, row: dict[str, Any]) -> str | None:
        return parse_time(row.get("revtime"))

    def normalize(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"business_key_hash": digest(self.business_key(row)), "vessel_code": clean(row.get("vesselcode")), "vessel_en_name": normalize_name(row.get("vesselename")), "voyage": clean(row.get("voyage")), "terminal_code": clean(row.get("matou")), "direction": clean(row.get("ioflag")), "ctn_start_time": parse_time(row.get("ctnstart")), "ctn_end_time": parse_time(row.get("ctnend")), "ports_raw": clean(row.get("ediports")), "vessel_operator": clean(row.get("vesselowner")), "source_update_time": self.source_update_time(row), "ingested_at": now_utc(), "record_hash": digest(stable(row)), "quality_json": stable({}), "raw_json": stable(row)}

    def persist(self, row: dict[str, Any]) -> tuple[bool, bool]:
        normalized = self.normalize(row)
        self.store.upsert_dimensions(normalized)
        self.store.upsert_bronze(self.endpoint_name, self.business_key(row), normalized, normalized["ctn_start_time"], normalized["source_update_time"])
        return self.store.insert_notice(normalized)


def run_phase1(config: Config) -> dict[str, Any]:
    with TimeseriesStore(config.db_path) as store, NpediClient(config) as client:
        return {"vessel_plan": VesselPlanCrawler(client, store, config).crawl(), "container_notice": ContainerNoticeCrawler(client, store, config).crawl()}
