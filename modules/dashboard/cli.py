"""模块⑤ CLI：把各模块产出聚合成前端契约。

用法：python3 -m modules.dashboard.cli
"""

from __future__ import annotations

import argparse

from core.cli import base_parser, output_dir, project_path, setup_logging

from .aggregate import build_contract, write_contract

log = setup_logging(__name__)


def main() -> None:
    parser = base_parser("大盘汇报：聚合五大模块产出为前端契约")
    parser.add_argument("--intel-dir", default=str(output_dir("intel")))
    parser.add_argument("--api-dir", default=str(project_path("api")))
    parser.add_argument("--tracker", default=str(output_dir("jd", "applications.json")))
    args = parser.parse_args()

    contract = build_contract(args.cache, args.intel_dir, args.tracker)
    manifest = write_contract(contract, args.api_dir)
    log.info("契约已生成: %s（%d 个区块）", manifest["files"][0], len(manifest["sections"]))
    print(f"大盘契约: {manifest['generated_at']} | 区块 {', '.join(manifest['sections'])}")


if __name__ == "__main__":
    main()
