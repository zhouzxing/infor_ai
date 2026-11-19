"""监控中心②：爬取工具稳定性与反爬诊断。"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from core.http import classify_error


def diagnose(cache_data: Dict) -> Dict:
    """把源状态与四大采集器的错误归类，给出可执行的改进建议。"""
    reasons: Counter = Counter()
    examples: Dict[str, List[str]] = {}
    sources_status = cache_data.get("meta", {}).get("sources_status", {})
    for name, status in sources_status.items():
        if status.get("status") not in ("lost", "retrying"):
            continue
        reason = classify_error(status.get("last_err", ""))
        reasons[reason] += 1
        examples.setdefault(reason, [])
        if len(examples[reason]) < 5:
            examples[reason].append(f"{name}: {status.get('last_err', '')[:100]}")

    tool_errors: Dict[str, List[str]] = {}
    for key in ("github_repos", "arxiv_papers", "jd_jobs"):
        errors = cache_data.get(key, {}).get("errors", [])
        tool_errors[key] = list(errors or [])
        for err in errors or []:
            reason = classify_error(str(err))
            reasons[reason] += 1
            examples.setdefault(reason, []).append(str(err)[:120])

    suggestions = {
        "反爬拦截：403": "更新 UA/Cookie、接入浏览器指纹或改用官方 API / RSS 镜像。",
        "反爬拦截：429": "降低 workers、增大请求间隔、为失败源加指数退避。",
        "网络/代理不可达": "检查 PROXY_HTTP/PROXY_HTTPS 与代理进程；可临时直连验证。",
        "页面结构变更": "更新解析规则（JD __INITIAL_STATE__ / RSS 字段），并补一条解析单测。",
        "鉴权失败": "检查 gh auth login / API Token 权限。",
    }
    improvements = []
    for reason in reasons:
        for key, advice in suggestions.items():
            if reason.startswith(key):
                improvements.append({"reason": reason, "advice": advice})
                break

    failures = [
        {"reason": reason, "count": count, "examples": examples.get(reason, [])[:5]}
        for reason, count in reasons.most_common()
    ]
    return {
        "failures": failures,
        "tool_errors": tool_errors,
        "improvements": improvements,
        "health": {
            "sources_with_error": sum(1 for s in sources_status.values()
                                      if s.get("status") in ("lost", "retrying")),
            "total_sources": len(sources_status),
            "tool_error_count": sum(len(v) for v in tool_errors.values()),
        },
    }
