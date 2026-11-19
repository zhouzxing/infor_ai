"""模块① CLI：make 通过本入口调用各阶段。

用法：python3 -m modules.intel_network.cli --action {collect,analyze,forecast,rankings,all}
"""

from __future__ import annotations

import argparse
from pathlib import Path

from core.cli import base_parser, output_dir, setup_logging

from .analyze import analyze_news
from .collect import fetch_all, fetch_single
from .forecast import build_forecast
from .rankings import build_rankings

log = setup_logging(__name__)


def run(action: str, cache: str, data_dir: str, workers: int, deadline: int) -> None:
    outdir = Path(data_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    if action in ("collect", "all"):
        fetch_all(cache, workers=workers, deadline=deadline)
    if action in ("analyze", "all"):
        analyze_news(cache, str(outdir / "intel_news.json"))
    if action in ("forecast", "all"):
        from core.store import get_cache

        entries = get_cache(cache).get_entries("int") + get_cache(cache).get_entries("cn")
        build_forecast(entries, str(outdir / "intel_forecast.json"))
    if action in ("rankings", "all"):
        build_rankings(cache, str(outdir / "intel_rankings.json"))
    log.info("模块① %s 完成", action)


def main() -> None:
    parser = base_parser("资讯网络：收集 / 分析 / 预判 / 排行榜")
    parser.add_argument("--action", default="all",
                        choices=["collect", "analyze", "forecast", "rankings", "all"])
    parser.add_argument("--data-dir", default=str(output_dir("intel")))
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--deadline", type=int, default=420)
    parser.add_argument("--source", help="仅抓取单个源（需 --action collect）")
    args = parser.parse_args()

    if args.source:
        entries, err = fetch_single(args.source, args.cache)
        print(f"{'OK' if not err else 'ERR'} {args.source}: {len(entries)} 条 {err or ''}")
        return
    run(args.action, args.cache, args.data_dir, args.workers, args.deadline)


if __name__ == "__main__":
    main()
