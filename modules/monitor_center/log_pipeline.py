"""监控中心③：日志管线（后台定时任务 + 前后端运行日志审计）。"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Dict, List

from core.cli import now_iso


def _tail(path: Path, lines: int = 50, max_chars: int = 300) -> List[str]:
    """bounded-memory tail：只读文件尾部，不把整个日志载入内存。"""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        block = 8192
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            fh.seek(size)
            data = fh.read(step) + data
    text = data.decode("utf-8", errors="replace")
    return [line[:max_chars] for line in text.splitlines()[-lines:]]


def collect_logs(cache_data: Dict, log_dir: str | Path, tail: int = 12,
                 tail_file_lines: int = 30) -> Dict:
    run_log = cache_data.get("meta", {}).get("run_log", [])[-tail:]
    log_path = Path(log_dir)
    files = []
    if log_path.exists():
        for path in sorted(log_path.glob("*.log")):
            files.append({
                "name": path.name,
                "size_kb": round(path.stat().st_size / 1024, 1),
                "lines": _tail(path, tail_file_lines),
            })

    total_runs = len(cache_data.get("meta", {}).get("run_log", []))
    failed_runs = sum(1 for r in cache_data.get("meta", {}).get("run_log", [])
                      if r.get("errors", 0) > 0)
    return {
        "generated_at": now_iso(),
        "run_log": run_log,
        "files": files,
        "audit": {
            "total_runs": total_runs,
            "failed_runs": failed_runs,
            "success_rate": round(1 - failed_runs / total_runs, 3) if total_runs else None,
            "last_run": run_log[-1].get("t", "") if run_log else "",
        },
    }
