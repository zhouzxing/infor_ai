"""模块⑤ 大盘汇报：后端只产出数据契约，前端是纯静态文件。

契约文件：
- api/data.js        window.INTEL_DATA = {...}（file:// 离线可用，前后端唯一接口）
- api/contract.json  同内容的可读 JSON（审阅 / 调试 / 未来 REST API 替换用）
- api/manifest.json  生成时间、版本、模块产出是否齐全
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from core.cli import now_iso, project_path, read_json, write_json
from core.data_loader import themes
from core.store import get_cache

CONTRACT_VERSION = "1.0"


def _load(intel_dir: str | Path, name: str) -> Dict:
    return read_json(Path(intel_dir) / name, {}) or {}


def build_contract(cache_path: str, intel_dir: str, tracker_path: str) -> Dict:
    cache = get_cache(cache_path).load()
    news = _load(intel_dir, "intel_news.json")
    forecast = _load(intel_dir, "intel_forecast.json")
    rankings = _load(intel_dir, "intel_rankings.json")
    agent = _load(intel_dir, "intel_agent.json")
    jd = _load(intel_dir, "intel_jd.json")
    monitor = _load(intel_dir, "monitor.json")
    resume = _load(intel_dir, "resume_advice.json")
    applications = read_json(tracker_path, {}) or {}

    # ① 一眼总览
    kpi = {
        "资讯总量": len(cache["entries_int"]) + len(cache["entries_cn"]),
        "高热资讯": news.get("hot_entries_count", 0),
        "活跃数据源": monitor.get("kpi", {}).get("active_sources", 0),
        "数据源总数": monitor.get("kpi", {}).get("total_sources", 42),
        "模型数量": rankings.get("model_vendors", {}).get("total_models", 0),
        "Agent平台": rankings.get("agent_vendors", {}).get("total_platforms", 0),
        "GitHub热库": len(cache["github_repos"]["items"]),
        "arXiv论文": len(cache["arxiv_papers"]["items"]),
        "JD岗位": jd.get("total_jobs", 0),
    }

    # ② 投资 / 市场机会：来自预判 + 厂商动态 + 高薪方向
    opportunities: List[Dict] = []
    for prediction in forecast.get("predictions", []):
        opportunities.append({
            "title": prediction["topic"],
            "category": prediction["type"],
            "detail": prediction.get("rationale", ""),
            "confidence": prediction.get("confidence", 0),
            "source": "规则预判",
        })
    for event in agent.get("business", {}).get("events_by_type", {}).get("估值", [])[:5]:
        opportunities.append({
            "title": f"{event['vendor']}：{event['headline']}",
            "category": "厂商估值",
            "detail": event.get("detail", ""), "confidence": None,
            "source": "商业动态（待核验）",
        })
    for skill, count in (jd.get("top_skills") or [])[:5]:
        opportunities.append({
            "title": f"高需求技能：{skill}",
            "category": "人才方向",
            "detail": f"在 {count} 个 JD 中出现；平均目标薪资 {jd.get('top_salaries', [{}])[0].get('avg_k', 'N/A')}k",
            "confidence": None, "source": "JD 分析",
        })

    # ③ 预警：行业敏感度信号 + 失联源 + 反爬诊断
    alerts: List[Dict] = []
    for signal in forecast.get("signals", []):
        if signal["type"] == "行业敏感度":
            alerts.append({
                "level": "关注", "title": f"{signal['keyword']}：{signal['title'][:60]}",
                "detail": f"热度 {signal['heat']}，来源 {', '.join(signal['sources'][:3])}",
                "source": "预判信号",
            })
    for failure in monitor.get("crawler", {}).get("failures", [])[:8]:
        alerts.append({
            "level": "高" if failure["count"] > 5 else "中",
            "title": failure["reason"],
            "detail": f"命中 {failure['count']} 次；示例：{(failure.get('examples') or [''])[0][:100]}",
            "source": "反爬诊断",
        })
    for name in monitor.get("source_value", {}).get("lost_sources", [])[:8]:
        alerts.append({"level": "高", "title": f"数据源失联：{name}",
                       "detail": "超过 stale 阈值未成功抓取", "source": "数据源稳定性"})

    # ④ 行业 / 技术动向
    industry = {
        "trend_7d": news.get("trend_7d", {}),
        "top_keywords": (news.get("top_keywords") or [])[:20],
        "signals": (forecast.get("signals") or [])[:20],
        "sensitivity": forecast.get("sensitivity", {}),
        "research_directions": agent.get("rd", {}).get("direction_distribution", []),
        "hot_papers": (agent.get("rd", {}).get("hot_papers") or [])[:10],
        "data_notice": agent.get("data_notice", ""),
    }

    # ⑤ 人才储备
    talent = {
        "total_jobs": jd.get("total_jobs", 0),
        "hot_jobs": jd.get("hot_jobs", 0),
        "top_skills": (jd.get("top_skills") or [])[:20],
        "salary_brackets": jd.get("salary_brackets", {}),
        "top_salaries": (jd.get("top_salaries") or [])[:10],
        "city_distribution": jd.get("city_distribution", {}),
        "resume_coverage": resume.get("coverage"),
        "resume_missing": (resume.get("missing_skills") or [])[:10],
        "applications": applications.get("total", 0),
        "application_status": applications.get("by_status", {}),
        "note": "投递台账为本地 dry-run，不自动向外部站点提交。",
    }

    # ⑥ 榜单（收敛到可展示规模）
    vendor_rankings = rankings.get("model_vendors", {})
    agent_rankings = rankings.get("agent_vendors", {})
    oss = rankings.get("oss_community", {}).get("buckets", {})
    leaderboards = {
        "model_vendors": vendor_rankings.get("vendors", [])[:12],
        "top_models": vendor_rankings.get("top_models", [])[:12],
        "agent_vendors": agent_rankings.get("platforms", [])[:12],
        "agent_categories": agent_rankings.get("category_distribution", {}),
        "oss_community": {
            name: {"desc": bucket.get("desc", ""), "count": bucket.get("count", 0),
                   "items": bucket.get("items", [])[:8]}
            for name, bucket in oss.items()
        },
    }

    theme_cfg = themes()
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": now_iso(),
        "source_updated": cache["meta"].get("updated", ""),
        "kpi": kpi,
        "opportunities": opportunities[:20],
        "alerts": alerts[:20],
        "industry": industry,
        "talent": talent,
        "rankings": leaderboards,
        "business_events": agent.get("business", {}).get("events_by_type", {}),
        "monitor": {
            "kpi": monitor.get("kpi", {}),
            "channels": (monitor.get("channels") or [])[:42],
            "improvements": monitor.get("crawler", {}).get("improvements", []),
            "log_audit": monitor.get("logs", {}).get("audit", {}),
        },
        "themes": theme_cfg["themes"],
        "sections": theme_cfg["sections"],
        "default_theme": theme_cfg["default_theme"],
    }


def write_contract(contract: Dict, api_dir: str | Path) -> Dict:
    """写出前后端契约三件套。"""
    out = Path(api_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(contract, ensure_ascii=False, separators=(",", ":"))

    js_file = out / "data.js"
    js_file.write_text(f"/* 由 modules.dashboard 自动生成，前端唯一数据源；勿手改 */\n"
                       f"window.INTEL_DATA = {payload};\n", encoding="utf-8")

    write_json(out / "contract.json", contract)
    manifest = {
        "contract_version": contract["contract_version"],
        "generated_at": contract["generated_at"],
        "files": ["data.js", "contract.json", "manifest.json"],
        "sections": [s["id"] for s in contract["sections"]],
        "module_outputs": [
            "intel_news.json", "intel_forecast.json", "intel_rankings.json",
            "intel_agent.json", "intel_jd.json", "monitor.json",
        ],
        "frontend_entry": str(project_path("frontend", "index.html")),
    }
    write_json(out / "manifest.json", manifest)
    return manifest
