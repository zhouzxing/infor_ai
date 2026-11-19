#!/usr/bin/env python3
"""状态摘要：make status 调用。直接执行时也能正确找到项目根。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cli import project_path, read_json  # noqa: E402
from core.store import get_cache  # noqa: E402


def main() -> None:
    cache = get_cache(str(project_path("output", "cache", "ai_intel_cache.json")))
    stats = cache.stats()
    meta = cache.load()["meta"]
    status = meta.get("sources_status", {})
    counts = {}
    for item in status.values():
        counts[item.get("status", "pending")] = counts.get(item.get("status", "pending"), 0) + 1

    print("=== 缓存摘要 ===")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print("=== 数据源状态 ===")
    print("  " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "暂无记录"))
    manifest = read_json(project_path("api", "manifest.json"), None)
    print("=== 前端契约 ===")
    print(f"  {'已生成 ' + manifest['generated_at'] if manifest else '未生成，请运行 make build-api'}")


if __name__ == "__main__":
    main()
