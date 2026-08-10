"""Phase 2 crawlers for VGM, bulk release, transshipment and history enrichment."""
from __future__ import annotations

from typing import Any, Iterable

from coverage import ensure_container_state, mark_enrichment
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
            ensure_container_state(self.store, container_no, source=self.endpoint_name)
            self.store.conn.execute("""INSERT INTO container_enrichment_queue(container_no,priority,source,first_seen_at,status) VALUES(?,?,?,?,?)
            ON CONFLICT(container_no) DO UPDATE SET source=excluded.source""", (container_no, 0, self.endpoint_name, now_utc(), "pending"))
            self.store.commit()
        return result


class VgmCrawler(FactCrawler):
    endpoint_name = "vgm"
    mode = "backfill"
    fact_table = "fact_container_vgm"
    fact_fields = ("business_key_hash","container_no","vessel_code","vessel_name_raw","voyage","terminal_code","operator_code","direction","container_type","vgm_weight_kg","vgm_method","operator_time","terminal_received_time","result_code","result_description","sender_code","receiver_code","ingested_at","record_hash","raw_json")

    def build_requests(self, context: dict[str, Any]) -> Iterable[RequestSpec]:
        for item in context.get("container_nos", []):
            container_no = clean(item)
            if container_no:
                # The live endpoint accepts containerNumber; vessel filters returned HTTP 400.
                # One container can have multiple VGM observations. Keep the
                # partition per container, but fetch all of its rows in a
                # normal-sized page instead of issuing one request per row.
                yield RequestSpec(
                    f"container:{container_no}",
                    {"containerNumber": container_no},
                    self.config.page_size,
                )

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        return self.client.vgm_page(page, page_size=request.page_size, **request.filters)

    def normalize_fact(self, row: dict[str, Any]) -> dict[str, Any]:
        return normalize_vgm(row)

    def on_request_success(self, request: RequestSpec) -> None:
        mark_enrichment(
            self.store, request.filters["containerNumber"], "vgm", success=True
        )

    def on_request_failure(self, request: RequestSpec) -> None:
        mark_enrichment(
            self.store, request.filters["containerNumber"], "vgm",
            success=False, error="VGM request did not complete",
        )


class CargoReleaseCrawler(FactCrawler):
    endpoint_name = "cargo_release"
    mode = "backfill"
    fact_table = "fact_cargo_release"
    fact_fields = ("business_key_hash","vessel_code","vessel_name_raw","voyage","direction","bill_no","pass_time","terminal_code","flag","cargo_volume","gross_weight","piece_count","gross_weight_kg","gross_weight_unit","weight_rule_version","ingested_at","record_hash","raw_json")

    def build_requests(self, context: dict[str, Any]) -> Iterable[RequestSpec]:
        # The live endpoint rejects an empty vessel selection. Container notice
        # is the compact, already-collected source of vessel/voyage pairs and
        # avoids expanding a request for every historical vessel-plan snapshot.
        rows = self.store.conn.execute("""SELECT vessel_code, voyage
            FROM silver_container_notice
            WHERE vessel_code IS NOT NULL AND TRIM(vessel_code)<>''
              AND voyage IS NOT NULL AND TRIM(voyage)<>''
            GROUP BY vessel_code, voyage
            ORDER BY vessel_code, voyage""")
        for row in rows:
            vessel_code, voyage = clean(row[0]), clean(row[1])
            if vessel_code and voyage:
                yield RequestSpec(
                    f"vessel:{vessel_code}:{voyage}",
                    {"vesselcode": vessel_code, "voyage": voyage},
                    self.config.page_size,
                )

    def fetch_page(self, request: RequestSpec, page: int) -> dict[str, Any]:
        return self.client.cargo_release_page(page, page_size=request.page_size, **request.filters)

    def validate_response(self, request: RequestSpec, response: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
        """Stop if the API returns rows outside the requested vessel/voyage.

        The live endpoint has accepted these filters while ignoring them.  A
        client-side filter would still require downloading the entire global
        result once per vessel/voyage, so reject the response instead.
        """
        if not rows:
            return None

        expected_vessel = clean(request.filters.get("vesselcode")) or ""
        expected_voyage = clean(request.filters.get("voyage")) or ""
        for row in rows:
            actual_vessel = clean(row.get("vesselcode") or row.get("vesselCode")) or ""
            actual_voyage = clean(row.get("voyage")) or ""
            if actual_vessel != expected_vessel or actual_voyage != expected_voyage:
                return (
                    "cargo-release endpoint ignored filters: "
                    f"requested vesselcode={expected_vessel!r}, voyage={expected_voyage!r}; "
                    f"response contains vesselcode={actual_vessel!r}, voyage={actual_voyage!r}"
                )
        return None

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

    def on_request_success(self, request: RequestSpec) -> None:
        mark_enrichment(
            self.store, request.filters["container_no"], "history", success=True
        )
        self.store.conn.execute(
            """UPDATE container_enrichment_queue
               SET status='complete', last_attempt_at=?,
                   attempt_count=attempt_count+1, last_error=NULL
               WHERE container_no=?""",
            (now_utc(), request.filters["container_no"]),
        )
        self.store.commit()

    def on_request_failure(self, request: RequestSpec) -> None:
        mark_enrichment(
            self.store, request.filters["container_no"], "history",
            success=False, error="container-history request did not complete",
        )
        self.store.conn.execute(
            """UPDATE container_enrichment_queue
               SET status='pending', last_attempt_at=?,
                   attempt_count=attempt_count+1,
                   last_error='container-history request did not complete'
               WHERE container_no=?""",
            (now_utc(), request.filters["container_no"]),
        )
        self.store.commit()
