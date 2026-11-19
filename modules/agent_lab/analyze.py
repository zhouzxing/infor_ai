"""模块②分析：商业与研发动态。

商业：估值 / 营收 / 人事 / 产品线市场布局 / 收费变动。
研发：GitHub 热库 / arXiv 热论文 / 方向分布 / 高产作者。
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from core.cli import now_iso, write_json
from core.data_loader import company_dynamics, models
from core.store import get_cache


def analyze_agent(cache_path: str, output_path: str) -> Dict:
    cache_data = get_cache(cache_path).load()
    events = company_dynamics()

    by_type: Dict[str, List[Dict]] = {}
    for event in events:
        by_type.setdefault(event["event_type"], []).append({
            "vendor": event["vendor"], "region": event["region"],
            "headline": event["headline"], "detail": event["detail"],
            "date": event["date"], "tags": event["tags"],
        })

    # 收费变动：以 models.json 当前定价为基线（变化追踪需历史快照，见已知局限）
    pricing = []
    for vendor_id, vendor in models().items():
        for product in vendor.get("products", []):
            if product.get("pricing"):
                pricing.append({
                    "vendor": vendor.get("name", vendor_id),
                    "model": product.get("name", ""),
                    "pricing": product["pricing"],
                    "license": product.get("license", ""),
                })
    pricing.sort(key=lambda p: p["vendor"])

    repos = cache_data.get("github_repos", {}).get("items", [])
    arxiv = cache_data.get("arxiv_papers", {})
    papers = arxiv.get("items", [])
    hot_repos = sorted(repos, key=lambda r: -r.get("stars", 0))[:20]
    rising_repos = [r for r in repos if r.get("rising")][:15]
    hot_papers = sorted(papers, key=lambda p: -p.get("heat", 0))[:20]

    result = {
        "generated_at": now_iso(),
        "data_notice": "公司动态为人工整理示例（verified=false），实时化需接入资讯抓取与二次核验",
        "business": {
            "events_by_type": by_type,
            "event_count": len(events),
            "pricing_snapshot": pricing,
            "pricing_change_method": "当前仅快照；接入历史快照后按 (vendor, model) 比对定价字符串",
        },
        "rd": {
            "hot_repos": [
                {"full_name": r.get("full_name"), "lang": r.get("lang"),
                 "stars": r.get("stars", 0), "forks": r.get("forks", 0),
                 "pushed": r.get("pushed", ""), "desc": r.get("desc", "")[:120],
                 "url": r.get("url", "")}
                for r in hot_repos
            ],
            "rising_repos": [r.get("full_name", "") for r in rising_repos],
            "repo_language_distribution": dict(Counter(r.get("lang") or "unknown" for r in repos)),
            "hot_papers": [
                {"title": p.get("title", "")[:160], "direction": p.get("direction", ""),
                 "heat": p.get("heat", 0), "published": p.get("published", ""),
                 "url": p.get("abs_url", "")}
                for p in hot_papers
            ],
            "direction_distribution": arxiv.get("directions", []),
            "top_authors": arxiv.get("top_authors", []),
            "hot_paper_count": arxiv.get("hot_count", 0),
        },
    }
    write_json(output_path, result)
    return result
