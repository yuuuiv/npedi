"""Phase 2 crawlers for VGM, bulk release, transshipment and history enrichment."""
from __future__ import annotations

from typing import Any, Iterable

from normalize import normalize_cargo_release, normalize_container_event, normalize_transshipment, normalize_vgm
from timeseries import BaseCrawler, RequestSpec, TimeseriesStore, clean, now_utc


class FactCrawler(BaseCrawler):
    fact_table = ""
    fact_fields: tuple[str, ...] = ()

    def normalize_fact(self, row: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def persist(self, row: dict[str, Any]) -> tuple[bool, bool]:
        normalized = self.normalize_fact(row)
        self.store.upsert_bronze(self.endpoint_name, normalized["business_key_hash"], normalized, normalized.get("operator_time") or normalized.get("pass_time") or normalized.get("sailing_date"), None)
        result = self.store.upsert_fact(self.fact_table, normalized, self.fact_fields)
        container_no = normalized.get("container_no")
        if container_no:
            self.store.conn.execute("""INSERT INTO container_enrichment_queue(container_no,priority,source,first_seen_at,status) VALUES(?,?,?,?,?)
            ON CONFLICT(container_no) DO UPDATE SET source=excluded.source""", (container_no, 0, self.endpoint_name, now_utc(), "pending"))
            self.store.conn.commit()
        return result


class VgmCrawler(FactCrawler):
    endpoint_name = "vgm"
    mode = "backfill"
    fact_table = "fact_container_vgm"
    fact_fields = ("business_key_hash","container_no","vessel_code","vessel_name_raw","voyage","terminal_code","operator_code","direction","container_type","vgm_weight_kg","vgm_method","operator_time","terminal_received_time","result_code","result_description","sender_code","receiver_code","ingested_at","record_hash","raw_json")

    def build_requests(self, context: dict[str, Any]) -> Iterable[RequestSpec]:
        for item in context.get("vessels", []):
            vessel = item if isinstance(item, str) else item.get("vesselUnCode")
            if vessel:
                yield RequestSpec(f"vessel:{vessel}", {"vessel": vessel}, context.get("page_size", self.config.page_size))

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        return self.client.vgm_page(page, page_size=request.page_size, **request.filters)

    def normalize_fact(self, row: dict[str, Any]) -> dict[str, Any]:
        return normalize_vgm(row)


class CargoReleaseCrawler(FactCrawler):
    endpoint_name = "cargo_release"
    mode = "backfill"
    fact_table = "fact_cargo_release"
    fact_fields = ("business_key_hash","vessel_code","vessel_name_raw","voyage","direction","bill_no","pass_time","terminal_code","flag","cargo_volume","gross_weight","ingested_at","record_hash","raw_json")

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        return self.client.cargo_release_page(page, page_size=request.page_size, **request.filters)

    def normalize_fact(self, row: dict[str, Any]) -> dict[str, Any]:
        return normalize_cargo_release(row)


class TransshipmentCrawler(FactCrawler):
    endpoint_name = "transshipment"
    mode = "backfill"
    fact_table = "fact_transshipment"
    fact_fields = ("business_key_hash","container_no","operator_code","bill_no","quantity","weight","volume","cargo_description_raw","cargo_group_name","cargo_match_type","first_vessel_code","first_voyage","first_load_port_code","first_discharge_port_code","second_vessel_code","second_voyage","second_trans_port_code","sailing_date","cutoff_date","terminal_code","check_flag","send_flag","ingested_at","record_hash","raw_json")

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        return self.client.transshipment_page(page, page_size=request.page_size, **request.filters)

    def normalize_fact(self, row: dict[str, Any]) -> dict[str, Any]:
        return normalize_transshipment(row)


class ContainerHistoryCrawler(FactCrawler):
    endpoint_name = "container_history"
    mode = "enrichment"
    fact_table = "fact_container_event"
    fact_fields = ("business_key_hash","container_no","event_type","event_code_raw","event_time","vessel_code","voyage","terminal_code","direction","bill_no","event_source","event_confidence","source_record_hash","raw_json")

    def build_requests(self, context: dict[str, Any]) -> Iterable[RequestSpec]:
        for container_no in context.get("container_nos", []):
            if clean(container_no):
                yield RequestSpec(f"container:{container_no}", {"container_no": clean(container_no) or ""}, 1)

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        return self.client.container_history(request.filters["container_no"])

    def get_total(self, response: dict[str, Any]) -> int | None:
        return 0

    def normalize_fact(self, row: dict[str, Any]) -> dict[str, Any]:
        return normalize_container_event(row.get("ctnno") or "", row)

    def persist(self, row: dict[str, Any]) -> tuple[bool, bool]:
        normalized = self.normalize_fact(row)
        return self.store.upsert_fact(self.fact_table, normalized, self.fact_fields)
