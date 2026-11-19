"""缓存层：单一事实源的读写、去重、热度累加与状态记录。

不含抓取 / 分析 / 渲染逻辑。所有写操作都经 save() 原子落盘。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .cli import now_iso, read_json
from .data_loader import scoring
from .schema import empty_cache, migrate_cache, trim_entries, validate_cache

_MANAGERS: "OrderedDict[str, CacheManager]" = OrderedDict()


class CacheManager:
    """按 cache_path 维度管理一份缓存。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path.cwd() / "cache.json"
        self._cache: Optional[Dict] = None
        self.thresholds = scoring()["thresholds"]

    def load(self) -> Dict:
        if self._cache is None:
            cache = read_json(self.path)
            if not cache:
                self._cache = empty_cache()
                self.save()
            else:
                before = json.dumps(cache, sort_keys=True, ensure_ascii=False)
                self._cache = migrate_cache(cache)
                if json.dumps(self._cache, sort_keys=True, ensure_ascii=False) != before:
                    self.save()  # 迁移结果落盘，避免每次进程都重复迁移
        return self._cache

    def save(self) -> None:
        if self._cache is None:
            return
        from .cli import write_json

        write_json(self.path, self._cache)

    def get_entries(self, region: str) -> List[Dict]:
        key = "entries_int" if region == "int" else "entries_cn"
        return self.load().get(key, [])

    def add_entries(self, entries: List[Dict], region: str) -> int:
        """新增资讯：标题归一化去重 + 多源热度 +1（cap 5）+ 裁剪。返回新增条数。"""
        cache = self.load()
        key = "entries_int" if region == "int" else "entries_cn"
        existing = cache[key]

        def norm(title: str) -> str:
            return "".join(title.lower().split())[:80]

        index = {norm(e["title"]): e for e in existing}
        added = 0
        for entry in entries:
            k = norm(entry.get("title", ""))
            if k in index:
                target = index[k]
                target["heat"] = min(5, target.get("heat", 0) + 1)
                for src in entry.get("sources", []):
                    if src not in target.setdefault("sources", []):
                        target["sources"].append(src)
            else:
                existing.append(entry)
                index[k] = entry
                added += 1

        cache[key] = trim_entries(existing)
        cache["meta"]["updated"] = now_iso()
        self.save()
        return added

    def update_github(self, repos: List[Dict], orgs: List[str], errors: List[str]) -> None:
        cache = self.load()
        cache["github_repos"] = {
            "items": repos, "orgs": orgs, "updated": now_iso(), "errors": errors,
        }
        self.save()

    def update_arxiv(
        self, papers: List[Dict], directions: List[Dict], top_authors: List[Dict],
        hot_count: int, errors: List[str], days_window: int = 14,
    ) -> None:
        cache = self.load()
        cache["arxiv_papers"] = {
            "items": papers, "directions": directions, "top_authors": top_authors,
            "updated": now_iso(), "errors": errors, "hot_count": hot_count,
            "days_window": days_window, "direction_count": len(directions),
        }
        self.save()

    def update_jd(
        self, jobs: List[Dict], sources: List[str], cities: List[str],
        hot_count: int, big_count: int, keyword_count: int, errors: List[str],
    ) -> None:
        cache = self.load()
        cache["jd_jobs"] = {
            "items": jobs, "updated": now_iso(), "errors": errors,
            "sources": sources, "cities": cities, "hot_count": hot_count,
            "big_count": big_count, "keyword_count": keyword_count,
            "max_days": self.thresholds.get("jd_max_days", 15),
            "hot_days": self.thresholds.get("jd_hot_days", 3),
        }
        self.save()

    def record_source_status(
        self, name: str, region: str, url: str, ok: bool, count: int, err: str = "",
    ) -> None:
        """记录单次源抓取结果：保留 history（最近 N 条）+ 固化 status。"""
        cache = self.load()
        meta = cache["meta"]
        stamp = now_iso()
        keep = self.thresholds.get("source_history_keep", 48)

        history = meta.setdefault("sources_history", {}).setdefault(name, [])
        history.append({"t": stamp, "n": count, "ok": ok, "err": err})
        if len(history) > keep:
            del history[:-keep]

        status = meta.setdefault("sources_status", {}).setdefault(name, {
            "name": name, "region": region, "url": url, "total_items": 0,
            "status": "pending", "dot": "gray", "last_ok_t": None, "last_err": "",
            "stale": False, "history_ok": 0, "history_total": 0,
        })
        status["total_items"] += count
        status["history_total"] += 1
        if ok:
            status.update(status="active", dot="green", last_ok_t=stamp, last_err="")
            status["history_ok"] += 1
        else:
            status["status"] = "retrying" if status["history_ok"] > 0 else "lost"
            status["dot"] = "yellow" if status["status"] == "retrying" else "red"
            status["last_err"] = err[:200]

        stale_hours = self.thresholds.get("source_stale_hours", 24)
        if status["last_ok_t"]:
            last = datetime.fromisoformat(status["last_ok_t"])
            status["stale"] = (datetime.now() - last) > timedelta(hours=stale_hours)
            if status["stale"] and status["status"] == "active":
                status.update(status="lost", dot="red")
        else:
            status["stale"] = True

        meta["last_source_refresh"] = stamp
        self.save()

    def append_run_log(self, entry: Dict) -> None:
        cache = self.load()
        log = cache["meta"].setdefault("run_log", [])
        log.append(entry)
        if len(log) > 48:
            del log[:-48]
        self.save()

    def stats(self) -> Dict:
        cache = self.load()
        return {
            "entries_int": len(cache["entries_int"]),
            "entries_cn": len(cache["entries_cn"]),
            "github": len(cache["github_repos"]["items"]),
            "arxiv": len(cache["arxiv_papers"]["items"]),
            "jd": len(cache["jd_jobs"]["items"]),
            "sources": len(cache["meta"]["sources_status"]),
            "updated": cache["meta"]["updated"],
        }


def get_cache(path: str | Path | None = None) -> CacheManager:
    """按路径返回唯一 CacheManager 实例（不同路径互不污染）。"""
    resolved = str(Path(path).resolve()) if path else "default"
    if resolved not in _MANAGERS:
        if path is None:
            from .cli import project_path

            path = project_path("output", "cache", "ai_intel_cache.json")
        _MANAGERS[resolved] = CacheManager(path)
    return _MANAGERS[resolved]


def validate_path(path: str | Path) -> List[str]:
    data = read_json(path, None)
    if data is None:
        return [f"缓存文件不存在: {path}"]
    return validate_cache(data)
