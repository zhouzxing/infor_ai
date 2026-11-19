"""资讯分析：热度分布、来源贡献、7 日趋势、关键词频次。"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List

from core.cli import now_iso, write_json
from core.data_loader import scoring
from core.store import get_cache

from .forecast import extract_signals

# 中文连续词 + 英文/数字词（替代 0.1 中 Python 不支持的 \p{P} 正则）
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9.+-]{1,}")


def analyze_news(cache_path: str, output_path: str) -> Dict:
    cache = get_cache(cache_path)
    entries = cache.get_entries("int") + cache.get_entries("cn")

    heat_dist = Counter(e.get("heat", 0) for e in entries)
    source_contrib = Counter(src for e in entries for src in e.get("sources", []))
    daily = Counter(e.get("date", "") for e in entries if e.get("date"))
    trend = {day: daily[day] for day in sorted(daily)[-7:]}

    hot = [e for e in entries if e.get("heat", 0) >= scoring()["thresholds"]["news_hot_heat"]]
    hot.sort(key=lambda e: -e.get("heat", 0))

    stopwords = set(scoring()["news_stopwords"])
    keywords = Counter(
        token
        for e in entries
        for token in _TOKEN_RE.findall(e.get("title", ""))
        if token not in stopwords
    )

    result = {
        "generated_at": now_iso(),
        "total_entries": len(entries),
        "int_count": len(cache.get_entries("int")),
        "cn_count": len(cache.get_entries("cn")),
        "heat_distribution": dict(sorted(heat_dist.items())),
        "trend_7d": trend,
        "source_contribution": dict(source_contrib.most_common(20)),
        "top_keywords": keywords.most_common(30),
        "hot_entries_count": len(hot),
        "hot_entries_sample": [
            {"title": e["title"][:100], "heat": e["heat"], "date": e["date"],
             "sources": e["sources"]}
            for e in hot[:20]
        ],
        "signals": extract_signals(hot[:50]),
    }
    write_json(output_path, result)
    return result
