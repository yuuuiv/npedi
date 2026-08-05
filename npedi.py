"""Command-line entry point for the NPEDI timeseries pipeline."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from aggregate import rebuild_gate_daily, rebuild_gold
from backtest import model_version_for_as_of
from change import detect_changes, snapshot_trends
from cluster import build_feature_windows, cluster_features
from config import Config, load_config
from coverage import coverage_status, enrichment_batch, ensure_container_state, seed_container_catalog
from crawlers import CargoReleaseCrawler, ContainerHistoryCrawler, TransshipmentCrawler, VgmCrawler
from curves import build_curves
from quality import quality_report, write_quality_report
from render import render_curves
from timeseries import ContainerNoticeCrawler, NpediClient, TimeseriesStore, VesselPlanCrawler


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="npedi", description="NPEDI history, timeseries and analysis pipeline")
    p.add_argument("--db", type=Path, help="SQLite path")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)
    crawl = sub.add_parser("crawl")
    crawl.add_argument("target", choices=("vessel-plan", "container-notice", "vgm", "cargo-release", "transshipment", "container-history"))
    crawl.add_argument("--mode", default="incremental")
    crawl.add_argument("--resume", action="store_true")
    crawl.add_argument("--limit", type=int, default=500, help="箱号批次大小（VGM/container-history）")
    crawl.add_argument("--offset", type=int, default=0, help="箱号批次偏移（VGM/container-history）")
    for name in ("normalize", "quality-report"):
        sub.add_parser(name)
    sub.add_parser("seed-container-catalog", help="从全量 gate_events 建立可恢复的箱号目录")
    sub.add_parser("coverage-status", help="查看全量箱目录、VGM 和轨迹覆盖率")
    aggregate = sub.add_parser("aggregate")
    aggregate.add_argument("--as-of", help="只使用该时刻之前已观测的数据")
    gate_aggregate = sub.add_parser("aggregate-gate", help="只重建 CODECO 闸口日聚合")
    gate_aggregate.add_argument("--as-of", help="只使用该时刻之前已观测的数据")
    curves = sub.add_parser("build-curves")
    curves.add_argument("--as-of", help="只使用该时刻之前已观测的数据")
    curves.add_argument(
        "--granularity", choices=("day", "week", "all"), default="all",
        help="只重建日曲线、周曲线或两者（默认 all）",
    )
    render = sub.add_parser("render")
    render.add_argument("--as-of", help="渲染指定 as-of 曲线")
    render.add_argument("--granularity", choices=("day", "week", "all"), default="all", help="只渲染日线或周线")
    render.add_argument("--output", type=Path, help="HTML 输出路径")
    cluster = sub.add_parser("cluster")
    cluster.add_argument("--entity", default="terminal")
    cluster.add_argument("--curve-type", default="vgm")
    cluster.add_argument("--window", default="52w")
    cluster.add_argument("--algorithm", choices=("hierarchical", "hdbscan"), default="hierarchical")
    cluster.add_argument("--as-of", help="只使用该时刻之前已观测的数据")
    sub.add_parser("detect-changepoints")
    return p


def _config(args: argparse.Namespace) -> Config:
    cfg = load_config()
    if args.db:
        cfg.db_path = args.db.resolve()
    return cfg


def _dry(args: argparse.Namespace, cfg: Config) -> bool:
    if args.dry_run:
        print(json.dumps({"command": args.command, "db": str(cfg.db_path), "network": False}, ensure_ascii=False))
    return args.dry_run


def _vgm_container_batch(store: TimeseriesStore, limit: int, offset: int) -> tuple[str, list[str]]:
    limit, offset = max(1, limit), max(0, offset)
    rows = enrichment_batch(store, "vgm", limit, offset)
    if rows:
        return "container_enrichment_state", rows
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "container_enrichment_queue" in tables:
        rows = store.conn.execute("""SELECT container_no FROM container_enrichment_queue
            WHERE container_no IS NOT NULL AND TRIM(container_no)<>''
            ORDER BY first_seen_at, container_no LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        if rows:
            for row in rows:
                ensure_container_state(store, row[0], source="legacy_queue")
            store.conn.commit()
            return "container_enrichment_state", enrichment_batch(store, "vgm", limit, offset)
    if "containers" in tables:
        rows = store.conn.execute("""SELECT DISTINCT TRIM(containerno) FROM containers
            WHERE containerno IS NOT NULL AND TRIM(containerno)<>''
            ORDER BY TRIM(containerno) LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        return "containers", [r[0] for r in rows]
    return "none", []


def _container_history_batch(store: TimeseriesStore, limit: int, offset: int) -> list[str]:
    """Return a deterministic pending container-history batch.

    History enrichment uses the same queue as VGM. The rows remain pending so
    a failed or interrupted batch can be retried; callers use offset to move
    through the stable queue ordering.
    """
    limit, offset = max(1, limit), max(0, offset)
    rows = enrichment_batch(store, "history", limit, offset)
    if rows:
        return rows
    legacy = store.conn.execute("""SELECT container_no FROM container_enrichment_queue
        WHERE status='pending' AND container_no IS NOT NULL AND TRIM(container_no)<>''
        ORDER BY first_seen_at, container_no LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
    for row in legacy:
        ensure_container_state(store, row[0], source="legacy_queue")
    if legacy:
        store.conn.commit()
        return enrichment_batch(store, "history", limit, offset)
    return []


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    cfg = _config(args)
    if _dry(args, cfg):
        return 0
    with TimeseriesStore(cfg.db_path) as store:
        if args.command == "crawl":
            with NpediClient(cfg) as client:
                runners = {"vessel-plan": VesselPlanCrawler, "container-notice": ContainerNoticeCrawler, "vgm": VgmCrawler, "cargo-release": CargoReleaseCrawler, "transshipment": TransshipmentCrawler, "container-history": ContainerHistoryCrawler}
                crawler = runners[args.target](client, store, cfg)
                context = {}
                if args.target == "vgm":
                    source, container_nos = _vgm_container_batch(store, args.limit, args.offset)
                    context.update({"container_nos": container_nos, "container_source": source, "offset": args.offset})
                    if not container_nos:
                        logging.getLogger("npedi").warning("没有可用于 VGM 查询的箱号；先运行 transshipment，或确认旧 containers 表已存在")
                elif args.target == "container-history":
                    container_nos = _container_history_batch(store, args.limit, args.offset)
                    context.update({"container_nos": container_nos, "offset": args.offset})
                    if not container_nos:
                        logging.getLogger("npedi").warning("没有可用于 container history 的待处理箱号；先运行 transshipment 或 VGM")
                print(json.dumps(crawler.crawl(context, resume=args.resume), ensure_ascii=False))
        elif args.command == "normalize":
            print(json.dumps({"bronze_records": store.conn.execute("SELECT COUNT(*) FROM bronze_record").fetchone()[0], "note": "core crawlers normalize on ingest"}, ensure_ascii=False))
        elif args.command == "seed-container-catalog":
            logging.getLogger("npedi").info("building the full container catalog from gate_events")
            print(json.dumps(seed_container_catalog(store), ensure_ascii=False))
        elif args.command == "coverage-status":
            print(json.dumps(coverage_status(store), ensure_ascii=False, indent=2))
        elif args.command == "aggregate":
            logging.getLogger("npedi").info("starting trusted-window Gold aggregation")
            result = rebuild_gold(store, as_of=args.as_of)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "aggregate-gate":
            logging.getLogger("npedi").info("rebuilding CODECO event-type/TEU aggregates")
            print(json.dumps({"gate_daily": rebuild_gate_daily(store, as_of=args.as_of), "as_of": args.as_of}, ensure_ascii=False))
        elif args.command == "build-curves":
            day_count = 0
            week_count = 0
            if args.granularity in {"day", "all"}:
                day_count = build_curves(store, granularity="day", spi_weights=cfg.spi_weights, as_of=args.as_of)
            if args.granularity in {"week", "all"}:
                week_count = build_curves(store, granularity="week", spi_weights=cfg.spi_weights, as_of=args.as_of)
            print(json.dumps({"day": day_count, "week": week_count}, ensure_ascii=False))
        elif args.command == "cluster":
            rows = build_feature_windows(store, curve_type=args.curve_type, granularity="week", entity_type=args.entity, min_completeness=.8, as_of=args.as_of)
            print(json.dumps(cluster_features(store, rows, algorithm=args.algorithm, as_of=args.as_of), ensure_ascii=False))
        elif args.command == "detect-changepoints":
            print(json.dumps({"detect": detect_changes(store), "trend_snapshots": snapshot_trends(store)}, ensure_ascii=False))
        elif args.command == "quality-report":
            output = cfg.export_dir / "timeseries_quality.md"
            print(json.dumps(write_quality_report(store, output), ensure_ascii=False))
        elif args.command == "render":
            output = args.output or (cfg.export_dir / "timeseries_curves.html")
            granularity = None if args.granularity == "all" else args.granularity
            print(str(render_curves(
                store,
                output,
                model_version=model_version_for_as_of("v1", args.as_of),
                granularity=granularity,
                anchor_date=args.as_of,
            )))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
