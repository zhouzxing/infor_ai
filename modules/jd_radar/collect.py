"""模块③ JD 雷达：智联招聘 JD 抓取。

只负责请求 / 解析 / AI 相关性过滤 / 热度打分 / 写缓存；分析与简历优化在别处。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

from core.cli import now_iso, setup_logging
from core.data_loader import jd_config, scoring
from core.feed import strip_html
from core.http import http_get
from core.schema import JDJob
from core.store import get_cache

log = setup_logging(__name__)

SEARCH_API = "https://fe-api.zhaopin.com/c/i/sou?pageSize=90&cityId={city_id}&kw={keyword}&kt=3"
CITY_IDS = {"北京": "530", "上海": "538", "深圳": "765", "广州": "763", "杭州": "653"}
JD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://sou.zhaopin.com/",
    "Origin": "https://sou.zhaopin.com",
}


def parse_salary(value: str) -> Tuple[Optional[int], Optional[int]]:
    text = str(value).replace(" ", "").replace("K", "k").replace("千", "k")
    match = re.search(r"(\d+(?:\.\d+)?)k?\s*[-~]\s*(\d+(?:\.\d+)?)k?", text)
    if match:
        return int(float(match.group(1))), int(float(match.group(2)))
    match = re.search(r"(\d+(?:\.\d+)?)k", text)
    if match:
        val = int(float(match.group(1)))
        return val, val
    return None, None


def extract_skills(job: Dict) -> List[str]:
    skills: List[str] = []
    for key in ("skills", "jobTags", "tags", "keyword"):
        value = job.get(key)
        if isinstance(value, list):
            skills.extend(str(v) for v in value if v)
        elif isinstance(value, str):
            skills.extend(s.strip() for s in value.split(",") if s.strip())
    return sorted(set(skills))


def is_ai_job(name: str, skills: List[str], industry: str) -> bool:
    text = f"{name} {' '.join(skills)} {industry}".lower()
    return any(kw.lower() in text for kw in jd_config()["relevance"])


def heat_score(name: str, company: str, skills: List[str], keyword_hits: int,
               city: str, salary: str) -> int:
    heat = min(keyword_hits * 2, 10)
    if city in ("北京", "上海", "深圳"):
        heat += 2
    if any(big in company for big in scoring()["big_companies"]):
        heat += 5
    low, high = parse_salary(salary)
    if high and high >= scoring()["thresholds"]["jd_high_salary_k"]:
        heat += 3
    return min(heat, 20)


def fetch_keyword_city(keyword: str, city: str, max_pages: int = 3) -> Tuple[List[Dict], Optional[str]]:
    jobs: List[Dict] = []
    for page in range(1, max_pages + 1):
        url = SEARCH_API.format(
            city_id=CITY_IDS.get(city, "530"), keyword=urllib.parse.quote(keyword)
        ) + f"&pageNo={page}"
        content, err = http_get(url, headers=JD_HEADERS, max_retries=2)
        if err:
            return jobs, err
        try:
            data = json.loads(content or "")
            batch = data.get("data", {}).get("list", [])
        except json.JSONDecodeError:
            return jobs, "JSON 解析失败（智联返回结构变更或反爬拦截）"
        if not batch:
            break
        jobs.extend(batch)
        time.sleep(0.5)  # 礼貌间隔，降低 429 概率
    return jobs, None


def fetch_jd(cache_path: str, max_pages: int = 3) -> Dict:
    seen: Dict[Tuple[str, str, str], JDJob] = {}
    errors: List[str] = []
    relevance = jd_config()["relevance"]
    config = jd_config()

    for keyword in config["keywords"]:
        for city in config["cities"]:
            raw_jobs, err = fetch_keyword_city(keyword, city, max_pages)
            if err:
                errors.append(f"{keyword}/{city}: {err}")
                log.warning("JD %s/%s 失败: %s", keyword, city, err)
                continue

            count = 0
            for raw in raw_jobs:
                name = str(raw.get("jobName", "")).strip()
                company = str(raw.get("companyName", "")).strip()
                salary = str(raw.get("salary", "")).strip()
                skills = extract_skills(raw)
                industry = str(raw.get("industry", "")).strip()
                if not name or not is_ai_job(name, skills, industry):
                    continue

                hits = sum(1 for kw in relevance if kw.lower() in
                           f"{name} {' '.join(skills)} {industry}".lower())
                heat = heat_score(name, company, skills, hits, city, salary)
                key = (company, name, city)
                if key in seen:
                    if heat > seen[key].heat:
                        seen[key].heat = heat
                    continue

                working_exp = raw.get("workingExp")
                edu = raw.get("eduLevel")
                seen[key] = JDJob(
                    job_id=f"{company}_{name}_{city}_{keyword}".replace(" ", "_")[:64],
                    title=name, company=company, city=city, salary=salary,
                    experience=working_exp.get("name", "") if isinstance(working_exp, dict) else str(working_exp or ""),
                    education=edu.get("name", "") if isinstance(edu, dict) else str(edu or ""),
                    skills=skills, industry=industry,
                    description=strip_html(str(raw.get("jobDesc") or raw.get("description") or ""))[:2000],
                    url=str(raw.get("positionURL") or raw.get("url") or ""),
                    posted_date=str(raw.get("updateDate", ""))[:10] or now_iso()[:10],
                    keyword=keyword, heat=heat,
                    is_hot=heat >= scoring()["thresholds"]["jd_hot_heat"],
                    is_big_company=any(b in company for b in scoring()["big_companies"]),
                )
                count += 1
            log.info("JD %s/%s: %d 条（累计 %d）", keyword, city, count, len(seen))

    jobs = sorted(seen.values(), key=lambda j: (-j.heat, -j.is_hot, j.title))
    get_cache(cache_path).update_jd(
        [j.to_dict() for j in jobs],
        sorted({j.keyword for j in jobs}),
        sorted({j.city for j in jobs}),
        sum(j.is_hot for j in jobs), sum(j.is_big_company for j in jobs),
        len({j.keyword for j in jobs}), errors,
    )
    return {"jobs": len(jobs), "errors": errors}
