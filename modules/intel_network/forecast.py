"""预判引擎（规则版）：从高热资讯推导新市场 / 产品形态 / 功能特性 / 敏感度信号。

不调用 LLM，输出可解释：每条信号都带命中的关键词与原文证据。
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from core.cli import now_iso
from core.data_loader import scoring

SIGNAL_TYPES = ("新市场", "产品形态", "功能特性", "行业敏感度")


def extract_signals(hot_entries: List[Dict], limit: int = 50) -> List[Dict]:
    """从高热条目抽取（类型, 关键词）去重的预判信号。"""
    keywords_by_type = scoring()["signal_keywords"]
    seen: set = set()
    signals: List[Dict] = []
    for entry in hot_entries:
        title = entry.get("title", "")
        for signal_type in SIGNAL_TYPES:
            for keyword in keywords_by_type.get(signal_type, []):
                if keyword.lower() in title.lower():
                    key = (signal_type, keyword)
                    if key not in seen:
                        seen.add(key)
                        signals.append({
                            "type": signal_type,
                            "keyword": keyword,
                            "title": title[:120],
                            "heat": entry.get("heat", 0),
                            "date": entry.get("date", ""),
                            "sources": entry.get("sources", []),
                        })
                    break
    return signals[:limit]


def build_forecast(entries: List[Dict], output_path: str) -> Dict:
    """产出 signals + 规则预判 + 行业/产业/政策敏感度评分。"""
    hot = [e for e in entries if e.get("heat", 0) >= scoring()["thresholds"]["news_hot_heat"]]
    hot.sort(key=lambda e: -e.get("heat", 0))
    signals = extract_signals(hot)

    by_type = Counter(s["type"] for s in signals)
    predictions: List[Dict] = []
    rules = [
        ("新市场", 3, "赛道商业化加速", "多条高热资讯同时指向新市场/融资信号"),
        ("产品形态", 3, "新形态产品集中出现", "Agent/具身/多模态等形态被反复提及"),
        ("功能特性", 3, "能力竞赛升级", "长上下文/工具调用/推理成为高频差异点"),
        ("行业敏感度", 2, "政策与资本敏感度上升", "融资/监管/出海事件进入高热区"),
    ]
    for signal_type, threshold, topic, rationale in rules:
        if by_type.get(signal_type, 0) >= threshold:
            predictions.append({
                "topic": topic, "type": signal_type,
                "confidence": min(0.5 + by_type[signal_type] * 0.05, 0.9),
                "evidence_count": by_type[signal_type], "rationale": rationale,
            })

    industry = min(1.0, (by_type["新市场"] + by_type["产品形态"]) / 20)
    sector = min(1.0, by_type["功能特性"] / 15)
    policy = min(1.0, by_type["行业敏感度"] / 10)

    result = {
        "generated_at": now_iso(),
        "method": "rule_based（关键词命中 + 计数阈值，非 LLM 推断）",
        "signals": signals,
        "signal_counts": dict(by_type),
        "predictions": predictions,
        "sensitivity": {
            "行业": round(industry, 3),
            "产业": round(sector, 3),
            "政策": round(policy, 3),
        },
    }
    from core.cli import write_json

    write_json(output_path, result)
    return result
