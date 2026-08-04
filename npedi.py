"""Command-line entry point for the NPEDI timeseries pipeline."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from aggregate import rebuild_gold
from change import detect_changes, snapshot_trends
from cluster import build_feature_windows, cluster_features
from config import Config, load_config
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
    crawl.add_argument("--limit", type=int, default=500)
    crawl.add_argument("--offset", type=int, default=0)
    for name in ("normalize", "aggregate", "build-curves", "quality-report", "render"):
        sub.add_parser(name)
    cluster = sub.add_parser("cluster")
    cluster.add_argument("--entity", default="terminal")
    cluster.add_argument("--curve-type", default="vgm")
    cluster.add_argument("--window", default="52w")
    cluster.add_argument("--algorithm", choices=("hierarchical", "hdbscan"), default="hierarchical")
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
    tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "container_enrichment_queue" in tables:
        rows = store.conn.execute("""SELECT container_no FROM container_enrichment_queue
            WHERE container_no IS NOT NULL AND TRIM(container_no)<>''
            ORDER BY first_seen_at, container_no LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        if rows:
            return "container_enrichment_queue", [r[0] for r in rows]
    if "containers" in tables:
        rows = store.conn.execute("""SELECT DISTINCT TRIM(containerno) FROM containers
            WHERE containerno IS NOT NULL AND TRIM(containerno)<>''
            ORDER BY TRIM(containerno) LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        return "containers", [r[0] for r in rows]
    return "none", []


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
                    context["container_nos"] = [r[0] for r in store.conn.execute("SELECT container_no FROM container_enrichment_queue WHERE status='pending' LIMIT ?", (args.limit,))]
                print(json.dumps(crawler.crawl(context, resume=args.resume), ensure_ascii=False))
        elif args.command == "normalize":
            print(json.dumps({"bronze_records": store.conn.execute("SELECT COUNT(*) FROM bronze_record").fetchone()[0], "note": "core crawlers normalize on ingest"}, ensure_ascii=False))
        elif args.command == "aggregate":
            print(json.dumps(rebuild_gold(store), ensure_ascii=False))
        elif args.command == "build-curves":
            print(json.dumps({"day": build_curves(store, granularity="day", spi_weights=cfg.spi_weights), "week": build_curves(store, granularity="week", spi_weights=cfg.spi_weights)}, ensure_ascii=False))
        elif args.command == "cluster":
            rows = build_feature_windows(store, curve_type=args.curve_type, granularity="week", entity_type=args.entity, min_completeness=.8)
            print(json.dumps(cluster_features(store, rows, algorithm=args.algorithm), ensure_ascii=False))
        elif args.command == "detect-changepoints":
            print(json.dumps({"detect": detect_changes(store), "trend_snapshots": snapshot_trends(store)}, ensure_ascii=False))
        elif args.command == "quality-report":
            output = cfg.export_dir / "timeseries_quality.md"
            print(json.dumps(write_quality_report(store, output), ensure_ascii=False))
        elif args.command == "render":
            output = cfg.export_dir / "timeseries_curves.html"
            print(str(render_curves(store, output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
