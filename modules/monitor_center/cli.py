"""模块④ CLI：生成监控大盘数据。

用法：python3 -m modules.monitor_center.cli
"""

from __future__ import annotations

import argparse

from core.cli import base_parser, output_dir, project_path, setup_logging

from .aggregate import generate_monitor

log = setup_logging(__name__)


def main() -> None:
    parser = base_parser("监控中心：数据源 / 爬虫稳定性 / 日志管线")
    parser.add_argument("--data", default=str(output_dir("intel", "monitor.json")))
    parser.add_argument("--log-dir", default=str(project_path("output", "logs")))
    args = parser.parse_args()
    result = generate_monitor(args.cache, args.data, args.log_dir)
    log.info("监控数据: %d 渠道, %d 失败项",
             len(result["channels"]), len(result["crawler"]["failures"]))
    print(f"监控: 活跃 {result['kpi']['active_sources']}/{result['kpi']['total_sources']} 源")


if __name__ == "__main__":
    main()
