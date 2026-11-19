"""模块③ JD 雷达：热门 / 高薪 / 主力方向分析。"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from core.cli import now_iso, write_json
from core.data_loader import scoring
from core.store import get_cache

from .collect import parse_salary


def analyze_jd(cache_path: str, output_path: str) -> Dict:
    jobs = get_cache(cache_path).load().get("jd_jobs", {}).get("items", [])
    thresholds = scoring()["thresholds"]
    if not jobs:
        result = {"generated_at": now_iso(), "total_jobs": 0,
                  "note": "暂无 JD 数据（智联反爬/未执行 fetch-jd）"}
        write_json(output_path, result)
        return result

    salary_rows: List[Dict] = []
    for job in jobs:
        low, high = parse_salary(job.get("salary", ""))
        if low is None or high is None:
            continue
        salary_rows.append({
            "title": job.get("title", ""), "company": job.get("company", ""),
            "city": job.get("city", ""), "salary": job.get("salary", ""),
            "min_k": low, "max_k": high, "avg_k": (low + high) / 2,
            "url": job.get("url", ""),
        })
    salary_rows.sort(key=lambda row: -row["avg_k"])

    brackets = Counter()
    for row in salary_rows:
        avg = row["avg_k"]
        brackets["<20k" if avg < 20 else "20-30k" if avg < 30 else
                 "30-50k" if avg < 50 else "50-80k" if avg < 80 else "80k+"] += 1

    skills = Counter(s.strip() for job in jobs for s in job.get("skills", []) if s.strip())
    hot = [j for j in jobs if j.get("heat", 0) >= thresholds["jd_hot_heat"]]
    big = [j for j in jobs if j.get("is_big_company")]

    result = {
        "generated_at": now_iso(),
        "total_jobs": len(jobs),
        "hot_jobs": len(hot),
        "big_company_jobs": len(big),
        "keyword_distribution": dict(Counter(j.get("keyword", "") for j in jobs)),
        "city_distribution": dict(Counter(j.get("city", "") for j in jobs)),
        "salary_brackets": dict(brackets),
        "top_salaries": salary_rows[:20],
        "top_skills": skills.most_common(50),
        "experience_distribution": dict(Counter(j.get("experience", "") for j in jobs)),
        "education_distribution": dict(Counter(j.get("education", "") for j in jobs)),
        "top_companies": Counter(j.get("company", "") for j in jobs).most_common(20),
        "hot_jobs_sample": [
            {"title": j.get("title"), "company": j.get("company"), "city": j.get("city"),
             "salary": j.get("salary"), "heat": j.get("heat"), "skills": j.get("skills", [])[:10],
             "url": j.get("url", "")}
            for j in sorted(hot, key=lambda x: -x.get("heat", 0))[:20]
        ],
        "main_directions": [name for name, _ in skills.most_common(10)],
    }
    write_json(output_path, result)
    return result
