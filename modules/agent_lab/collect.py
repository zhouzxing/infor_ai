"""模块② Agent = Model + Harness：GitHub 热库 / arXiv 论文采集。

只抓取、归一化、写缓存；商业与研发分析在 analyze.py。
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.cli import now_iso, setup_logging
from core.data_loader import arxiv_directions, arxiv_sources, github_config, scoring
from core.feed import normalize_date, parse_feed, strip_html
from core.http import http_get
from core.schema import ArxivPaper, GhRepo
from core.store import get_cache

log = setup_logging(__name__)


# ─── GitHub 热榜 ───────────────────────────────────────────
def run_gh(cmd: List[str], timeout: int = 60) -> Tuple[Optional[str], Optional[str]]:
    env = {"GH_PAGER": "cat"}
    import os

    full_env = os.environ.copy()
    full_env.update(env)
    try:
        result = subprocess.run(["gh"] + cmd, capture_output=True, text=True,
                                timeout=timeout, env=full_env)
        if result.returncode != 0:
            return None, result.stderr.strip()[:200]
        return result.stdout.strip(), None
    except subprocess.TimeoutExpired:
        return None, "gh CLI timeout"
    except FileNotFoundError:
        return None, "gh CLI 未安装"


def parse_gh_output(output: str) -> List[Dict]:
    try:
        parsed = json.loads(output)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        items = []
        for line in output.splitlines():
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items


def normalize_repo(raw: Dict) -> Optional[GhRepo]:
    owner_obj = raw.get("owner", {})
    owner = owner_obj.get("login", "") if isinstance(owner_obj, dict) else str(owner_obj)
    owner_type = "org" if isinstance(owner_obj, dict) and owner_obj.get("type") == "Organization" else "user"
    try:
        return GhRepo(
            name=raw.get("name", ""), full_name=raw.get("full_name", ""),
            owner=owner, owner_type=owner_type, lang=raw.get("language"),
            stars=int(raw.get("stargazers_count", 0) or 0),
            forks=int(raw.get("forks_count", 0) or 0),
            created=str(raw.get("created_at", ""))[:10],
            pushed=str(raw.get("pushed_at", ""))[:10],
            rising=False, org_pick=False,
            desc=raw.get("description") or "", url=raw.get("html_url", ""),
            topics=raw.get("topics", []) or [],
        )
    except (TypeError, ValueError):
        return None


def is_ai_repo(repo: Dict) -> bool:
    text = " ".join([
        str(repo.get("name", "")), str(repo.get("description", "")),
        " ".join(repo.get("topics", []) or []),
    ]).lower()
    return any(kw in text for kw in scoring()["github_ai_keywords"])


def _fetch_topic(topic: str, per_page: int = 50) -> List[Dict]:
    cmd = ["api", f"/search/repositories?q=topic:{topic}+stars:>100&sort=stars&order=desc&per_page={per_page}",
           "--jq", ".items[] | {name,full_name,owner,stargazers_count,forks_count,language,created_at,pushed_at,description,topics,html_url}"]
    out, err = run_gh(cmd)
    if err:
        log.warning("GitHub topic %s 失败: %s", topic, err)
        return []
    return parse_gh_output(out or "")


def _fetch_rising(per_page: int = 50) -> List[Dict]:
    since = github_config()["rising_since"]
    cmd = ["api", f"/search/repositories?q=created:>{since}+stars:>50&sort=stars&order=desc&per_page={per_page}",
           "--jq", ".items[] | {name,full_name,owner,stargazers_count,forks_count,language,created_at,pushed_at,description,topics,html_url}"]
    out, err = run_gh(cmd)
    if err:
        log.warning("GitHub rising 失败: %s", err)
        return []
    return parse_gh_output(out or "")


def _fetch_org(org: str, per_page: int = 10) -> List[Dict]:
    cmd = ["api", f"/orgs/{org}/repos?sort=stars&per_page={per_page}",
           "--jq", ".[] | {name,full_name,owner,stargazers_count,forks_count,language,created_at,pushed_at,description,topics,html_url}"]
    out, err = run_gh(cmd)
    if err:
        log.warning("GitHub org %s 失败: %s", org, err)
        return []
    return parse_gh_output(out or "")


def fetch_github(cache_path: str) -> Dict:
    """三路抓取（主题榜 / 新星 / 组织精选）合并去重后写缓存。"""
    all_repos: Dict[str, GhRepo] = {}
    errors: List[str] = []

    for topic in github_config()["topics"]:
        for raw in _fetch_topic(topic):
            if not is_ai_repo(raw):
                continue
            repo = normalize_repo(raw)
            if repo:
                all_repos.setdefault(repo.full_name, repo)

    for raw in _fetch_rising():
        if not is_ai_repo(raw):
            continue
        repo = normalize_repo(raw)
        if repo:
            if repo.full_name in all_repos:
                all_repos[repo.full_name].rising = True
            else:
                repo.rising = True
                all_repos[repo.full_name] = repo

    for org in github_config()["orgs"]:
        for raw in _fetch_org(org):
            if not is_ai_repo(raw):
                continue
            repo = normalize_repo(raw)
            if repo:
                if repo.full_name in all_repos:
                    all_repos[repo.full_name].org_pick = True
                else:
                    repo.org_pick = True
                    all_repos[repo.full_name] = repo

    repos = sorted(all_repos.values(), key=lambda r: (-r.stars, -r.forks))
    cache = get_cache(cache_path)
    cache.update_github([r.to_dict() for r in repos], github_config()["orgs"], errors)
    cache.append_run_log({
        "t": now_iso(), "module": "agent_lab.github", "src_ok": 1 if repos else 0,
        "src_total": 1, "items_new": len(repos), "gh_items": len(repos),
    })
    log.info("GitHub 热榜: %d 仓库（新星 %d）", len(repos), sum(r.rising for r in repos))
    return {"repos": len(repos), "rising": sum(r.rising for r in repos), "errors": errors}


# ─── arXiv 论文 ───────────────────────────────────────────
def classify_direction(title: str, summary: str, categories: List[str]) -> Tuple[str, str, str, int, List[str]]:
    text = " ".join([title, summary, " ".join(categories)]).lower()
    best_tag, best_hits, matched = "other", 0, []
    for direction in arxiv_directions():
        hits = sum(1 for kw in direction["keywords"] if kw.lower() in text)
        if hits > best_hits:
            best_hits, best_tag = hits, direction["tag"]
        if hits > 0:
            matched.append(direction["tag"])
    chosen = next((d for d in arxiv_directions() if d["tag"] == best_tag), None)
    if chosen:
        return chosen["name"], chosen["tag"], chosen["desc"], best_hits, matched
    return "其他", "other", "未命中任何方向", 0, []


def calculate_heat(title: str, direction_hits: int, days_old: int) -> int:
    heat = min(direction_hits * 2, 10)
    heat += 5 if days_old <= 1 else 3 if days_old <= 3 else 1 if days_old <= 7 else 0
    for keyword in ("agent", "reasoning", "multimodal", "diffusion", "rlhf", "moe", "world model", "embodied"):
        if keyword in title.lower():
            heat += 2
    return min(heat, 20)


def fetch_arxiv(cache_path: str, days_window: int = 14) -> Dict:
    papers: List[ArxivPaper] = []
    errors: List[str] = []
    cutoff = datetime.now() - timedelta(days=days_window)

    for source in arxiv_sources():
        content, err = http_get(source["url"])
        if err:
            errors.append(f"{source['name']}: {err}")
            log.warning("arXiv %s 失败: %s", source["name"], err)
            continue
        count = 0
        for item in parse_feed(content, source["url"]):
            published = normalize_date(item.published)
            if published:
                try:
                    if datetime.fromisoformat(published) < cutoff:
                        continue
                except ValueError:
                    pass

            direction, tag, desc, hits, matched = classify_direction(
                item.title, item.description, item.tags
            )
            days_old = 0
            if published:
                try:
                    days_old = max(0, (datetime.now() - datetime.fromisoformat(published)).days)
                except ValueError:
                    pass

            heat = calculate_heat(item.title, hits, days_old)
            arxiv_id = item.link.rsplit("/", 1)[-1] if item.link else ""
            paper = ArxivPaper(
                arxiv_id=arxiv_id, base_id=arxiv_id.split("v")[0] if "v" in arxiv_id else arxiv_id,
                title=item.title, summary=strip_html(item.description)[:500],
                published=published,
                authors=[a.strip() for a in item.author.split(",") if a.strip()] if item.author else [],
                primary_category=item.tags[0] if item.tags else "", categories=item.tags,
                comment=item.description[:200], abs_url=item.link,
                pdf_url=item.link.replace("/abs/", "/pdf/") + ".pdf" if item.link else "",
                direction=direction, direction_tag=tag, direction_desc=desc,
                direction_hits=hits, direction_tags=matched,
                is_hot=heat >= scoring()["thresholds"]["arxiv_hot_heat"],
                days_old=days_old, heat=heat,
            )
            papers.append(paper)
            count += 1
        log.info("arXiv %s: %d 篇", source["name"], count)

    dedup: Dict[str, ArxivPaper] = {}
    for paper in papers:
        if paper.base_id not in dedup or paper.heat > dedup[paper.base_id].heat:
            dedup[paper.base_id] = paper
    unique = sorted(dedup.values(), key=lambda p: (-p.heat, -p.days_old))

    direction_counts = Counter(p.direction for p in unique)
    directions = [
        {"name": d["name"], "tag": d["tag"], "desc": d["desc"],
         "count": direction_counts.get(d["name"], 0)}
        for d in arxiv_directions()
    ]
    author_counts = Counter(a for p in unique for a in p.authors)
    top_authors = [{"name": n, "papers": c} for n, c in author_counts.most_common(20)]
    hot_count = sum(1 for p in unique if p.is_hot)

    get_cache(cache_path).update_arxiv(
        [p.to_dict() for p in unique], directions, top_authors, hot_count, errors, days_window
    )
    log.info("arXiv 完成: %d 篇（热门 %d）", len(unique), hot_count)
    return {"papers": len(unique), "hot": hot_count, "errors": errors}


def fetch_all(cache_path: str) -> Dict:
    return {"github": fetch_github(cache_path), "arxiv": fetch_arxiv(cache_path)}
