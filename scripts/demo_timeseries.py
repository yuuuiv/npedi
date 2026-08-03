from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggregate import rebuild_gold
from config import Config
from crawlers import CargoReleaseCrawler, TransshipmentCrawler, VgmCrawler
from curves import build_curves
from quality import write_quality_report
from render import render_curves
from timeseries import ContainerNoticeCrawler, TimeseriesStore, VesselPlanCrawler

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


class DemoClient:
    def __init__(self):
        self.plan = [json.loads((FIX / "vessel_plan_page_1.json").read_text(encoding="utf-8")), json.loads((FIX / "vessel_plan_page_2.json").read_text(encoding="utf-8"))]
        self.notice = json.loads((FIX / "container_notice_page_1.json").read_text(encoding="utf-8"))
        self.facts = json.loads((FIX / "core_facts.json").read_text(encoding="utf-8"))

    def vessel_plan_page(self, page, **_):
        return self.plan[page - 1] if page <= len(self.plan) else {"code": 200, "data": {"total": 3, "list": []}}

    def container_notice_page(self, page, **_):
        return self.notice if page == 1 else {"code": 200, "data": {"total": 2, "list": []}}

    def vgm_page(self, page, **_):
        return self.facts["vgm"]

    def cargo_release_page(self, page, **_):
        return self.facts["release"]

    def transshipment_page(self, page, **_):
        return self.facts["transshipment"]

    def container_history(self, container_no):
        return self.facts["history"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=ROOT / "export" / "timeseries_demo")
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cfg = Config(token="fixture", db_path=args.output / "demo.sqlite", page_size=2, request_delay=(0, 0), max_pages_per_query=5)
    client = DemoClient()
    with TimeseriesStore(cfg.db_path) as store:
        VesselPlanCrawler(client, store, cfg).crawl()
        ContainerNoticeCrawler(client, store, cfg).crawl()
        VgmCrawler(client, store, cfg).crawl({"vessels": ["UN0000001"]})
        CargoReleaseCrawler(client, store, cfg).crawl()
        TransshipmentCrawler(client, store, cfg).crawl()
        rebuild_gold(store)
        curves = build_curves(store, granularity="day", spi_weights=cfg.spi_weights)
        report = write_quality_report(store, args.output / "quality.md")
        render_curves(store, args.output / "curves.html")
    print(json.dumps({"output": str(args.output.resolve()), "curves": curves, "quality_gate": report["summary"]["quality_gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

