"""模块① 资讯网络：情报收集、监控、分析、预判、排行榜。

职责边界：
- collect.py   抓取 RSS/Atom → 写缓存（唯一写入方）
- analyze.py   读缓存 → 热度/趋势/关键词
- forecast.py  读高热条目 → 新市场/产品形态/功能特性/敏感度预判
- rankings.py  读 data/*.json + 缓存 → 模型厂商/Agent厂商/开源热度四类榜单
- cli.py       Makefile 调用入口
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

from core.cli import now_iso, setup_logging
from core.data_loader import sources
from core.feed import fetch_rss_feed
from core.store import get_cache

log = setup_logging(__name__)


def fetch_all(cache_path: str, workers: int = 10, deadline: int = 420) -> Dict:
    """并发抓取全部资讯源；逐源记录状态与运行日志。"""
    cache = get_cache(cache_path)
    started = time.time()
    by_region: Dict[str, List[Dict]] = {"int": [], "cn": []}
    errors: List[str] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_rss_feed, s["name"], s["url"], s["keywords"], s["region"]): s
            for s in sources()
        }
        for future in as_completed(futures):
            source = futures[future]
            if time.time() - started > deadline:
                for pending in futures:
                    pending.cancel()
                errors.append(f"全局预算 {deadline}s 耗尽，取消剩余任务")
                log.warning(errors[-1])
                break
            try:
                entries, err = future.result()
            except Exception as exc:  # 单源失败不影响其他源
                entries, err = [], f"{type(exc).__name__}: {exc}"

            cache.record_source_status(
                source["name"], source["region"], source["url"],
                ok=not err, count=len(entries), err=err or "",
            )
            if err:
                errors.append(f"{source['name']}: {err}")
                log.warning("源失败 %s: %s", source["name"], err)
            elif entries:
                added = cache.add_entries(entries, source["region"])
                by_region[source["region"]].extend(entries)
                log.info("源 %s: %d 条（新增 %d）", source["name"], len(entries), added)

    summary = {
        "total": sum(len(v) for v in by_region.values()),
        "int": len(by_region["int"]),
        "cn": len(by_region["cn"]),
        "errors": errors,
        "elapsed_s": round(time.time() - started, 1),
    }
    cache.append_run_log({
        "t": now_iso(), "module": "intel_network", "src_ok": len(sources()) - len(errors),
        "src_total": len(sources()), "items_new": summary["total"], "errors": len(errors),
    })
    log.info("资讯抓取完成: %s", summary)
    return summary


def fetch_single(name: str, cache_path: str) -> Tuple[List[Dict], str | None]:
    source = next((s for s in sources() if s["name"] == name), None)
    if not source:
        return [], f"未知数据源: {name}"
    entries, err = fetch_rss_feed(
        source["name"], source["url"], source["keywords"], source["region"]
    )
    cache = get_cache(cache_path)
    cache.record_source_status(
        name, source["region"], source["url"], ok=not err,
        count=len(entries), err=err or "",
    )
    if entries:
        cache.add_entries(entries, source["region"])
    return entries, err
