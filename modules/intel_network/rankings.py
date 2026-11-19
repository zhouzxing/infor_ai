"""排行榜：模型厂商 / Agent 厂商 / 开源社区热度（科研 · 工程 · homelab · 教育培训）。

数据源全部来自 data/*.json 与缓存，榜单生成是纯函数式变换，便于 code review 与单测。
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from core.cli import now_iso, write_json
from core.data_loader import agent_platforms, models, scoring
from core.scoring import score_all_products
from core.store import get_cache


def model_vendor_ranking() -> Dict:
    """模型厂商榜：API 服务 / 线上服务 / 产品能力（六维加权）。"""
    scored = score_all_products(models())
    vendors: Dict[str, Dict] = {}
    for row in scored:
        vendor = vendors.setdefault(row["vendor_id"], {
            "vendor_id": row["vendor_id"], "vendor_name": row["vendor_name"],
            "region": row["region"], "model_count": 0, "avg_composite": 0.0,
            "top_model": "", "top_score": 0.0, "open_models": 0,
        })
        vendor["model_count"] += 1
        vendor["avg_composite"] += row["composite"]
        vendor["open_models"] += int("open" in row["license"].lower()
                                     or "apache" in row["license"].lower()
                                     or "mit" in row["license"].lower())
        if row["composite"] > vendor["top_score"]:
            vendor["top_score"] = row["composite"]
            vendor["top_model"] = row["model_name"]

    ranking = sorted(vendors.values(), key=lambda v: -v["avg_composite"])
    for vendor in ranking:
        vendor["avg_composite"] = round(vendor["avg_composite"] / max(1, vendor["model_count"]), 2)
    return {
        "generated_at": now_iso(),
        "vendors": ranking,
        "top_models": scored[:20],
        "total_models": len(scored),
    }


def agent_vendor_ranking() -> Dict:
    """Agent 厂商榜：按功能特性丰富度 + 免费层 + 分类覆盖排序。"""
    rows: List[Dict] = []
    for name, platform in agent_platforms().items():
        features = [f.strip() for f in str(platform.get("features", "")).split(",") if f.strip()]
        rows.append({
            "name": name,
            "region": platform.get("region", "?"),
            "category": platform.get("category", "未分类"),
            "pricing": platform.get("pricing", ""),
            "free": platform.get("free", ""),
            "feature_count": len(features),
            "features": features,
            "enterprise": platform.get("enterprise", ""),
        })
    rows.sort(key=lambda r: (-r["feature_count"], r["name"]))

    category_count = Counter(r["category"] for r in rows)
    free_count = Counter(r["free"] for r in rows)
    return {
        "generated_at": now_iso(),
        "total_platforms": len(rows),
        "category_distribution": dict(category_count),
        "free_tier_distribution": dict(free_count),
        "platforms": rows,
    }


def oss_community_ranking(repos: List[Dict], per_bucket: int = 15) -> Dict:
    """开源社区热度四分类：科研算法 / 工程落地 / homelab / 教育培训。"""
    categories = scoring()["oss_categories"]
    buckets: Dict[str, List[Dict]] = {name: [] for name in categories}
    buckets["其他"] = []

    for repo in repos:
        text = " ".join([
            str(repo.get("name", "")), str(repo.get("desc", "")),
            " ".join(repo.get("topics", []) or []),
        ]).lower()
        hits = {
            name: sum(1 for kw in cfg["keywords"] if kw.lower() in text)
            for name, cfg in categories.items()
        }
        best = max(hits, key=lambda k: hits[k])
        bucket = best if hits[best] > 0 else "其他"
        buckets[bucket].append({
            "full_name": repo.get("full_name", ""),
            "lang": repo.get("lang"),
            "stars": repo.get("stars", 0),
            "forks": repo.get("forks", 0),
            "pushed": repo.get("pushed", ""),
            "rising": repo.get("rising", False),
            "org_pick": repo.get("org_pick", False),
            "desc": repo.get("desc", "")[:120],
            "url": repo.get("url", ""),
            "matched": hits.get(bucket, 0),
        })

    result: Dict[str, Dict] = {}
    for name, items in buckets.items():
        items.sort(key=lambda r: (-r["stars"], -r["forks"]))
        result[name] = {
            "desc": categories.get(name, {}).get("desc", "未命中任何分类关键词"),
            "count": len(items),
            "items": items[:per_bucket],
        }
    return {"generated_at": now_iso(), "buckets": result}


def build_rankings(cache_path: str, output_path: str) -> Dict:
    cache = get_cache(cache_path)
    repos = cache.load().get("github_repos", {}).get("items", [])
    result = {
        "generated_at": now_iso(),
        "model_vendors": model_vendor_ranking(),
        "agent_vendors": agent_vendor_ranking(),
        "oss_community": oss_community_ranking(repos),
    }
    write_json(output_path, result)
    return result
