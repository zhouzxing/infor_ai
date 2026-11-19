"""六维能力评分：被「资讯网络·排行榜」和「Agent 实验室·商业动态」共用。

所有阈值/权重来自 data/scoring.json 与 data/models.json，代码中无硬编码权重。
"""

from __future__ import annotations

import re
from typing import Dict, List

from .data_loader import scoring


def parse_context(value: str) -> int:
    text = str(value).upper().replace(",", "")
    if "M" in text:
        return int(float(re.sub(r"[^0-9.]", "", text.split("M")[0]) or 0) * 1_000_000)
    if "K" in text:
        return int(float(re.sub(r"[^0-9.]", "", text.split("K")[0]) or 0) * 1_000)
    return 0


def parse_params(value: str) -> float:
    """返回 B（十亿）为单位的规模；MoE 取激活参数。"""
    text = str(value).lower()
    if "active" in text:
        match = re.search(r"(\d+(?:\.\d+)?)\s*b\s*active", text)
        if match:
            return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*([bt])", text)
    if not match:
        return 0.0
    val = float(match.group(1))
    return val * 1000 if match.group(2) == "t" else val


def parse_price(value: str) -> float:
    """返回输入/输出均价（$/1M tok）；免费/开源返回 0。"""
    nums = re.findall(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if len(nums) >= 2:
        return (float(nums[0]) + float(nums[1])) / 2
    return float(nums[0]) if nums else 0.0


def calculate_fresh(release: str) -> int:
    """迭代活跃度：2026 年内越晚发布分越高。"""
    match = re.match(r"(20\d{2})-(\d{2})", str(release))
    if not match:
        return 0
    year, month = int(match.group(1)), int(match.group(2))
    if year == 2026:
        return max(1, 13 - month)
    if year == 2025:
        return 1
    return 0


def score_product(product: Dict) -> Dict[str, float]:
    """六维评分（0-100）。"""
    ctx = parse_context(product.get("context", "0"))
    ctx_score = (
        100 if ctx >= 2_000_000 else 95 if ctx >= 1_000_000 else 80 if ctx >= 200_000
        else 70 if ctx >= 128_000 else 40 if ctx >= 32_000 else 20
    )

    mods = product.get("modalities", [])
    mm = 30
    if "vision" in mods or "image" in mods:
        mm += 20
    if "audio" in mods or "speech" in mods:
        mm += 15
    if "video" in mods:
        mm += 15
    if "code" in mods:
        mm += 10
    if "tool" in mods:
        mm += 10

    license_text = str(product.get("license", "")).lower()
    if "apache" in license_text or "mit" in license_text:
        open_score = 100
    elif "community" in license_text or "cc-by" in license_text:
        open_score = 60
    elif "open" in license_text:
        open_score = 40
    else:
        open_score = 20

    price = parse_price(product.get("pricing", ""))
    cost = (
        100 if price == 0 else 80 if price <= 1 else 60 if price <= 5
        else 40 if price <= 15 else 20
    )

    scale_value = parse_params(product.get("params", ""))
    scale = (
        90 if scale_value >= 400 else 75 if scale_value >= 200 else 60 if scale_value >= 100
        else 50 if scale_value >= 70 else 40 if scale_value >= 30 else 30 if scale_value >= 10 else 20
    )

    return {
        "ctx": float(ctx_score),
        "mm": float(min(mm, 100)),
        "open": float(open_score),
        "cost": float(cost),
        "scale": float(scale),
        "fresh": float(min(calculate_fresh(product.get("release", "")) * 20, 100)),
    }


def composite_score(scores: Dict[str, float], weights: Dict[str, float] | None = None) -> float:
    weights = weights or scoring()["dim_weights"]
    return round(sum(scores.get(dim, 0.0) * weight for dim, weight in weights.items()), 2)


def score_all_products(model_registry: Dict) -> List[Dict]:
    """对模型注册表全量打分并降序排序。"""
    weights = scoring()["dim_weights"]
    result: List[Dict] = []
    for vendor_id, vendor in model_registry.items():
        for product in vendor.get("products", []):
            scores = score_product(product)
            result.append({
                "vendor_id": vendor_id,
                "vendor_name": vendor.get("name", vendor_id),
                "region": vendor.get("region", "?"),
                "model_name": product.get("name", ""),
                "context": product.get("context", ""),
                "modalities": product.get("modalities", []),
                "pricing": product.get("pricing", ""),
                "params": product.get("params", ""),
                "license": product.get("license", ""),
                "release": product.get("release", ""),
                "scores": scores,
                "composite": composite_score(scores, weights),
            })
    result.sort(key=lambda row: -row["composite"])
    return result
