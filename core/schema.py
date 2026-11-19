"""数据契约：缓存结构 dataclass、空缓存工厂、Schema 校验。

字段命名与 0.1 版本保持一致，便于复用历史缓存（只做增量迁移）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Dict, List

SCHEMA_VERSION = "v10"

# 0.1 版本缓存里状态用过中文 / 不同灯色，迁移时归一到本版契约，避免监控误判全红。
STATUS_ALIAS = {"活跃": "active", "重试中": "retrying", "失联": "lost", "待同步": "pending"}
DOT_ALIAS = {"good": "green", "warn": "yellow", "warning": "yellow", "danger": "red", "bad": "red"}


# ─── 条目结构 ───────────────────────────────────────────────
@dataclass
class NewsEntry:
    title: str
    summary: str
    body: str
    url: str
    sources: List[str]
    heat: int
    timestamp: str
    date: str
    region: str

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class GhRepo:
    name: str
    full_name: str
    owner: str
    owner_type: str  # org | user
    lang: str | None
    stars: int
    forks: int
    created: str
    pushed: str
    rising: bool
    org_pick: bool
    desc: str
    url: str
    topics: List[str]

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ArxivPaper:
    arxiv_id: str
    base_id: str
    title: str
    summary: str
    published: str
    authors: List[str]
    primary_category: str
    categories: List[str]
    comment: str
    abs_url: str
    pdf_url: str
    direction: str
    direction_tag: str
    direction_desc: str
    direction_hits: int
    direction_tags: List[str]
    is_hot: bool
    days_old: int
    heat: int

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class JDJob:
    job_id: str
    title: str
    company: str
    city: str
    salary: str
    experience: str
    education: str
    skills: List[str]
    industry: str
    description: str
    url: str
    posted_date: str
    keyword: str
    heat: int
    is_hot: bool
    is_big_company: bool

    def to_dict(self) -> Dict:
        return asdict(self)


# ─── 缓存骨架与迁移 ─────────────────────────────────────────
def empty_cache() -> Dict:
    """单一事实源的标准空结构。"""
    return {
        "entries_int": [],
        "entries_cn": [],
        "meta": {
            "updated": "",
            "last_source_refresh": "",
            "schema_version": SCHEMA_VERSION,
            "sources_history": {},
            "sources_status": {},
            "run_log": [],
        },
        "github_repos": {"items": [], "orgs": [], "updated": "", "errors": []},
        "arxiv_papers": {
            "items": [], "directions": [], "top_authors": [], "updated": "",
            "errors": [], "hot_count": 0, "days_window": 14, "direction_count": 0,
        },
        "jd_jobs": {
            "items": [], "updated": "", "errors": [], "sources": [], "cities": [],
            "hot_count": 0, "big_count": 0, "keyword_count": 0,
            "max_days": 15, "hot_days": 3,
        },
    }


def migrate_cache(cache: Dict) -> Dict:
    """幂等迁移：补全缺失键、清洗 HTML 残片、补全派生字段。"""
    template = empty_cache()
    for key, value in template.items():
        if key not in cache:
            cache[key] = value
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
                cache[key].setdefault(sub_key, sub_value)

    for region_key in ("entries_int", "entries_cn"):
        for entry in cache.get(region_key, []):
            for field in ("summary", "body"):
                val = entry.get(field, "")
                if isinstance(val, str) and val.lstrip().startswith(
                    ("<section", "<div", "<nav", "<header", "<footer", "<aside", "<script", "<style")
                ):
                    entry[field] = ""
            if not entry.get("url") and entry.get("sources"):
                entry["url"] = entry["sources"][0]
            if not entry.get("date") and entry.get("timestamp"):
                entry["date"] = entry["timestamp"][:10]
            entry.setdefault("region", "int" if region_key == "entries_int" else "cn")

    # 0.1 样例缓存曾用中文状态 / 不同灯色，归一到本版契约，避免监控误判全红。
    for status in cache.get("meta", {}).get("sources_status", {}).values():
        status["status"] = STATUS_ALIAS.get(status.get("status"), status.get("status", "pending"))
        status["dot"] = DOT_ALIAS.get(status.get("dot"), status.get("dot", "gray"))
    return cache


def trim_entries(entries: List[Dict], max_days: int = 30, max_entries: int = 300) -> List[Dict]:
    """时间窗过滤 + 低热旧条目剔除 + 数量截断。"""
    cutoff = datetime.now() - timedelta(days=max_days)
    kept: List[Dict] = []
    for entry in entries:
        ts = entry.get("timestamp")
        if ts:
            try:
                if datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
                    continue
            except ValueError:
                pass
        kept.append(entry)

    kept.sort(key=lambda x: (-x.get("heat", 0), x.get("timestamp", "")), reverse=True)

    if len(kept) <= 100:
        return kept[:max_entries]

    old_cutoff = datetime.now() - timedelta(days=3)
    result: List[Dict] = []
    for entry in kept:
        if len(result) >= max_entries:
            break
        is_old = False
        ts = entry.get("timestamp")
        if ts:
            try:
                is_old = datetime.fromisoformat(ts.replace("Z", "+00:00")) < old_cutoff
            except ValueError:
                pass
        if not (is_old and entry.get("heat", 0) <= 1):
            result.append(entry)
    return result[:max_entries]


def validate_cache(cache: Dict) -> List[str]:
    """返回 Schema 错误列表；空列表表示通过。"""
    errors: List[str] = []
    required = set(empty_cache().keys())
    missing = required - set(cache.keys())
    if missing:
        errors.append(f"缺少顶层键: {sorted(missing)}")

    for region in ("entries_int", "entries_cn"):
        for i, entry in enumerate(cache.get(region, [])):
            fields = {"title", "summary", "url", "sources", "heat", "timestamp", "date", "region"}
            if not fields.issubset(entry):
                errors.append(f"{region}[{i}] 缺少字段")
            if not 0 <= entry.get("heat", -1) <= 5:
                errors.append(f"{region}[{i}] heat 越界")

    meta = cache.get("meta", {})
    for key in ("sources_status", "run_log"):
        if key not in meta:
            errors.append(f"meta.{key} 缺失")
    return errors
