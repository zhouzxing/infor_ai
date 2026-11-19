"""监控中心聚合：KPI + 渠道稳定性 + 反爬诊断 + 日志审计 + 数据源商业价值评估。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List

from core.cli import now_iso, write_json
from core.store import get_cache

from .crawler_health import diagnose
from .log_pipeline import collect_logs
from .source_health import build_channel_table


def _new_24h(entries: List[Dict]) -> int:
    cutoff = datetime.now() - timedelta(hours=24)
    count = 0
    for entry in entries:
        try:
            if datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")) >= cutoff:
                count += 1
        except (KeyError, ValueError):
            continue
    return count


def source_value_assessment(channels: List[Dict]) -> Dict:
    """数据源稳定性 / 免费质量 / 商业价值分层（当前仅免费公开源）。"""
    tiers = Counter(ch["tier"] for ch in channels)
    protocols = Counter(ch["protocol"] for ch in channels)
    lost = [ch["name"] for ch in channels if ch["status"] == "lost"]
    return {
        "tier_distribution": dict(tiers),
        "protocol_distribution": dict(protocols),
        "lost_sources": lost,
        "free_quality_note": "镜像源（RSSHub）稳定性弱于官方源；独家一手消息占比需人工标注后计入。",
        "commercial_value_note": "当前全部为免费公开源；付费/商业数据源未接入，商业价值评估待补采购清单。",
    }


def generate_monitor(cache_path: str, output_path: str, log_dir: str) -> Dict:
    cache_data = get_cache(cache_path).load()
    entries = cache_data["entries_int"] + cache_data["entries_cn"]
    channels = build_channel_table(cache_data)
    crawler = diagnose(cache_data)
    logs = collect_logs(cache_data, log_dir)

    status_values = [ch["status"] for ch in channels]
    result = {
        "generated_at": now_iso(),
        "kpi": {
            "total_entries": len(entries),
            "int_entries": len(cache_data["entries_int"]),
            "cn_entries": len(cache_data["entries_cn"]),
            "new_24h": _new_24h(entries),
            "total_sources": len(channels),
            "active_sources": status_values.count("active"),
            "retrying_sources": status_values.count("retrying"),
            "lost_sources": status_values.count("lost"),
            "github_repos": len(cache_data["github_repos"]["items"]),
            "arxiv_papers": len(cache_data["arxiv_papers"]["items"]),
            "jd_jobs": len(cache_data["jd_jobs"]["items"]),
        },
        "heat_distribution": dict(sorted(Counter(e.get("heat", 0) for e in entries).items())),
        "channels": channels,
        "crawler": crawler,
        "logs": logs,
        "source_value": source_value_assessment(channels),
    }
    write_json(output_path, result)
    return result
