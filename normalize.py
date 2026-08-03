"""Conservative Silver normalizers for the documented core endpoints."""
from __future__ import annotations

import re
from typing import Any, Iterable

from timeseries import clean, digest, normalize_name, now_utc, parse_number, parse_time, stable


def _key(values: Iterable[Any], row: dict[str, Any]) -> str:
    parts = [clean(v) or "" for v in values]
    base = "|".join(parts)
    return digest(base if any(parts) else stable(row))


def normalize_vgm(row: dict[str, Any]) -> dict[str, Any]:
    operator_time = parse_time(row.get("operatetime"))
    receive_time = parse_time(row.get("portreceiverdate"))
    quality = {}
    if clean(row.get("vgmGrossWeight")) and parse_number(row.get("vgmGrossWeight")) is None:
        quality["invalid_vgm_weight"] = True
    return {"business_key_hash": _key((row.get("containerNumber"), row.get("vesselcode"), row.get("voyage"), row.get("operatetime"), row.get("senderCode")), row), "container_no": clean(row.get("containerNumber")), "vessel_code": clean(row.get("vesselcode")), "vessel_name_raw": clean(row.get("vessel")), "voyage": clean(row.get("voyage")), "terminal_code": clean(row.get("receiverCode")), "operator_code": clean(row.get("ctnOperatorCode")), "direction": clean(row.get("containerType")), "container_type": clean(row.get("containerType")), "vgm_weight_kg": parse_number(row.get("vgmGrossWeight")), "vgm_method": clean(row.get("vgmMethod")), "operator_time": operator_time, "terminal_received_time": receive_time, "result_code": clean(row.get("resultCode")), "result_description": clean(row.get("resultDescripts")), "sender_code": clean(row.get("senderCode")), "receiver_code": clean(row.get("receiverCode")), "ingested_at": now_utc(), "record_hash": digest(stable(row)), "raw_json": stable(row), "quality_json": stable(quality)}


def normalize_cargo_release(row: dict[str, Any]) -> dict[str, Any]:
    return {"business_key_hash": _key((row.get("billno"), row.get("vesselcode"), row.get("voyage"), row.get("passtime"), row.get("cpcode")), row), "vessel_code": clean(row.get("vesselcode")), "vessel_name_raw": clean(row.get("envessel")), "voyage": clean(row.get("voyage")), "direction": clean(row.get("direct")), "bill_no": clean(row.get("billno")), "pass_time": parse_time(row.get("passtime")), "terminal_code": clean(row.get("cpcode")), "flag": clean(row.get("flag")), "cargo_volume": parse_number(row.get("cargovolum")), "gross_weight": parse_number(row.get("grossweight")), "ingested_at": now_utc(), "record_hash": digest(stable(row)), "raw_json": stable(row)}


def normalize_cargo_text(value: Any) -> str | None:
    text = clean(value)
    return " ".join(text.replace("　", " ").upper().split()) if text else None


def classify_cargo(value: Any, exact: dict[str, str] | None = None, patterns: Iterable[tuple[str, str]] = ()) -> tuple[str, str]:
    text = normalize_cargo_text(value)
    if not text:
        return "UNKNOWN", "unknown"
    exact = {normalize_cargo_text(k) or "": v for k, v in (exact or {}).items()}
    if text in exact:
        return exact[text], "exact"
    for pattern, group in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return group, "regex"
    return "UNKNOWN", "unknown"


def normalize_transshipment(row: dict[str, Any], exact: dict[str, str] | None = None, patterns: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    cargo_group, match_type = classify_cargo(row.get("cargoDescription"), exact, patterns)
    return {"business_key_hash": _key((row.get("ctnNo"), row.get("firstVesselCode"), row.get("firstVoyage"), row.get("secondVesselCode"), row.get("secondVoyage"), row.get("billNo")), row), "container_no": clean(row.get("ctnNo")), "operator_code": clean(row.get("ctnOperatorCode")), "bill_no": clean(row.get("billNo")), "quantity": parse_number(row.get("quantity")), "weight": parse_number(row.get("weight")), "volume": parse_number(row.get("volume")), "cargo_description_raw": clean(row.get("cargoDescription")), "cargo_group_name": cargo_group, "cargo_match_type": match_type, "first_vessel_code": clean(row.get("firstVesselCode")), "first_voyage": clean(row.get("firstVoyage")), "first_load_port_code": clean(row.get("firstLoadPortCode")), "first_discharge_port_code": clean(row.get("firstDischargePortCode")), "second_vessel_code": clean(row.get("secondVesselCode")), "second_voyage": clean(row.get("secondVoyage")), "second_trans_port_code": clean(row.get("secondTransPortCode")), "sailing_date": parse_time(row.get("sailingDate")), "cutoff_date": parse_time(row.get("cutOffDate")), "terminal_code": clean(row.get("terminal")), "check_flag": clean(row.get("checkFlag")), "send_flag": clean(row.get("sendFlag")), "ingested_at": now_utc(), "record_hash": digest(stable(row)), "raw_json": stable(row)}


def normalize_container_event(container_no: str, row: dict[str, Any]) -> dict[str, Any]:
    # operateId meanings are not verified; UNKNOWN is deliberate.
    return {"business_key_hash": _key((container_no, row.get("vesselcode"), row.get("voyage"), row.get("cpcode"), row.get("operateId"), row.get("blNo")), row), "container_no": clean(row.get("ctnno")) or container_no, "event_type": "UNKNOWN", "event_code_raw": clean(row.get("operateId")), "event_time": None, "vessel_code": clean(row.get("vesselcode")), "voyage": clean(row.get("voyage")), "terminal_code": clean(row.get("cpcode")), "direction": clean(row.get("direct")), "bill_no": clean(row.get("blNo")), "event_source": "ediContainerlog", "event_confidence": "unverified", "source_record_hash": digest(stable(row)), "raw_json": stable(row)}

