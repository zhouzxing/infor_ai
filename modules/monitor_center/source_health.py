"""监控中心①：数据源稳定性（42 渠道健康表）。"""

from __future__ import annotations

from typing import Dict, List

from core.data_loader import scoring, sources


def detect_protocol(url: str) -> str:
    lowered = url.lower()
    if "atom" in lowered:
        return "atom"
    if any(token in lowered for token in ("rss", "feed", "xml")):
        return "rss2"
    return "unknown"


def _consecutive_failures(history: List[Dict]) -> int:
    """从最近一次记录往前数连续失败次数（真值，不靠状态猜测）。"""
    failures = 0
    for record in reversed(history or []):
        if record.get("ok"):
            break
        failures += 1
    return failures


def build_channel_table(cache_data: Dict) -> List[Dict]:
    status_map = cache_data.get("meta", {}).get("sources_status", {})
    history_map = cache_data.get("meta", {}).get("sources_history", {})
    rows: List[Dict] = []
    for source in sources():
        name = source["name"]
        status = status_map.get(name, {})
        history = history_map.get(name, [])
        total, ok = status.get("history_total", 0), status.get("history_ok", 0)
        success_rate = f"{ok}/{total} ({ok / total * 100:.0f}%)" if total else "N/A"
        url = source["url"]
        tier = "镜像源（依赖 RSSHub 可用性）" if "rsshub" in url.lower() else "官方/公开源（一手）"
        rows.append({
            "name": name, "region": source["region"], "url": url,
            "strategy": "RSS 垂直" if not source["keywords"] else f"关键词({len(source['keywords'])})",
            "protocol": detect_protocol(url),
            "tier": tier,
            "total_items": status.get("total_items", 0),
            "success_rate": success_rate,
            "consecutive_fail": _consecutive_failures(history),
            "status": status.get("status", "pending"),
            "dot": status.get("dot", "gray"),
            "last_ok": status.get("last_ok_t", ""),
            "last_err": status.get("last_err", "")[:120],
        })
    return rows


def stale_threshold_hours() -> int:
    return scoring()["thresholds"]["source_stale_hours"]
