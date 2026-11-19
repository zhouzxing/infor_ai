"""JD 雷达：针对性简历优化（规则版）。

比较目标岗位技能栈与现有简历文本，输出覆盖率、缺口与可改写的 bullet 建议。
不编造经历，所有建议都要求用户填入真实项目/指标。
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from .analyze import parse_salary


def optimize_resume(resume_text: str, target_jobs: List[Dict], top_n: int = 15) -> Dict:
    skill_freq = Counter(
        s.strip()
        for job in target_jobs
        for s in job.get("skills", [])
        if s and s.strip()
    )
    required = [name for name, _ in skill_freq.most_common(top_n)]
    lowered = resume_text.lower()

    matched = [skill for skill in required if skill.lower() in lowered]
    missing = [skill for skill in required if skill not in matched]
    coverage = round(len(matched) / max(1, len(required)), 3)

    bullets = [
        f"补充「{skill}」相关经历：项目名 + 规模/数据量 + 你的动作 + 可量化结果（由本人填写真实指标）。"
        for skill in missing[:8]
    ]
    salary_values = []
    for job in target_jobs:
        low, high = parse_salary(job.get("salary", ""))
        if low and high:
            salary_values.append((low + high) / 2)

    return {
        "target_job_count": len(target_jobs),
        "required_skills": required,
        "matched_skills": matched,
        "missing_skills": missing,
        "coverage": coverage,
        "suggested_bullets": bullets,
        "avg_target_salary_k": round(sum(salary_values) / len(salary_values), 1) if salary_values else None,
        "note": "规则匹配仅作检索对齐；不代替事实核验，禁止照抄建议作为虚构经历。",
    }
