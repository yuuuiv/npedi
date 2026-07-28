"""CSV 导出层（对应 ARCHITECTURE.md §5.5）。

SQLite 是唯一真实数据源，CSV 是每轮同步结束后从库里导出的产物。
编码固定 UTF-8 with BOM（中文 Windows 上 Excel 直接双击不乱码）；
写入采用"临时文件 + 原子改名"，下游不会读到半个文件。
"""

from __future__ import annotations

import csv
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from config import Config
from store import CONTAINER_FIELDS, TIMESTAMP_FIELDS, Store

log = logging.getLogger("npedi.export")

_TS14 = re.compile(r"^\d{14}$")


def _fmt_ts(value: str) -> str:
    """20260727103201 → 2026-07-27 10:32:01。

    14 位纯数字在 Excel 里会被显示成科学计数法，转成 ISO 既避免这一点，
    也让 Excel 直接识别为日期时间；机器解析同样无歧义。
    """
    if not value or not _TS14.match(value):
        return value
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]} {value[8:10]}:{value[10:12]}:{value[12:14]}"


def _row_to_csv(row, fields: Sequence[str], iso_timestamps: bool) -> list[str]:
    out = []
    for f in fields:
        value = row[f]
        text = "" if value is None else str(value)
        if iso_timestamps and f in TIMESTAMP_FIELDS:
            text = _fmt_ts(text)
        out.append(text)
    return out


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]) -> int:
    """写临时文件后原子改名；返回数据行数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    count = 0
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    os.replace(tmp, path)       # 同分区内原子替换
    return count


def export_all(cfg: Config, store: Store, run_id: int | None = None) -> dict[str, str]:
    """导出全量快照 + 航次目录 + 本轮增量。返回 {文件名: 说明}。"""
    iso = cfg.csv_timestamp_format != "raw"
    written: dict[str, str] = {}
    export_dir = cfg.export_dir

    # 1) 全量当前态快照（每轮覆盖重写）
    snapshot_fields = list(CONTAINER_FIELDS) + ["first_seen_at", "updated_at"]
    n = _write_csv(
        export_dir / "containers_all.csv",
        snapshot_fields,
        (_row_to_csv(r, snapshot_fields, iso) for r in store.iter_containers()),
    )
    written["containers_all.csv"] = f"{n} 行（全量快照）"

    # 2) 航次目录（每轮覆盖重写）
    voyage_fields = [
        "unvessel", "voyage", "envessel", "portclose_at", "portclose_raw",
        "first_seen_at", "last_seen_at", "last_change_at", "backfilled",
    ]
    n = _write_csv(
        export_dir / "voyages.csv",
        voyage_fields,
        ([("" if r[f] is None else str(r[f])) for f in voyage_fields] for r in store.iter_voyages()),
    )
    written["voyages.csv"] = f"{n} 行（航次目录）"

    # 3) 本轮增量（无变化则不生成文件）
    if run_id is not None and store.run_change_count(run_id) > 0:
        change_fields = ["change_type"] + snapshot_fields
        name = f"changes_{datetime.now():%Y%m%d_%H%M%S}.csv"
        n = _write_csv(
            export_dir / name,
            change_fields,
            (_row_to_csv(r, change_fields, iso) for r in store.iter_run_changes(run_id)),
        )
        written[name] = f"{n} 行（本轮新增/变更）"
        _prune_change_files(export_dir, cfg.csv_keep_change_files)

    return written


def _prune_change_files(export_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(export_dir.glob("changes_*.csv"))
    for stale in files[:-keep]:
        try:
            stale.unlink()
        except OSError as exc:
            log.warning("清理旧增量文件失败 %s: %s", stale.name, exc)
