"""模块② Agent 实验室 CLI。

用法：python3 -m modules.agent_lab.cli --action {collect,analyze,all}
"""

from __future__ import annotations

import argparse

from core.cli import base_parser, output_dir, setup_logging

from .analyze import analyze_agent
from .collect import fetch_all

log = setup_logging(__name__)


def main() -> None:
    parser = base_parser("Agent 实验室：GitHub 热库 / arXiv 论文 / 商业与研发动态")
    parser.add_argument("--action", default="all", choices=["collect", "analyze", "all"])
    parser.add_argument("--data", default=str(output_dir("intel", "intel_agent.json")))
    parser.add_argument("--days", type=int, default=14, help="arXiv 时间窗口（天）")
    args = parser.parse_args()

    if args.action in ("collect", "all"):
        fetch_all(args.cache)
    if args.action in ("analyze", "all"):
        analyze_agent(args.cache, args.data)
    log.info("模块② %s 完成", args.action)


if __name__ == "__main__":
    main()
