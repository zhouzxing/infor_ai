"""投递与邀约管理：本地台账 + 投递材料包（dry-run）。

明确边界：本模块**不自动向外部网站提交**（那需要站点账号授权，且属于对外可见行为）。
它生成可审阅的投递清单/材料包，并记录状态流转；真实提交由人在授权后完成。
扩展点见 ARCHITECTURE.md「投递适配器」。
"""

from __future__ import annotations

from typing import Dict, List

from core.cli import now_iso, read_json, write_json

STATUSES = ["待投递", "已投递", "笔试", "面试", "offer", "拒绝"]


def empty_tracker() -> Dict:
    return {"updated": "", "applications": [], "invitations": []}


def load_tracker(path: str) -> Dict:
    return read_json(path, None) or empty_tracker()


def _save(tracker: Dict, path: str) -> Dict:
    tracker["updated"] = now_iso()
    write_json(path, tracker)
    return tracker


def plan_applications(jobs: List[Dict], tracker_path: str, limit: int = 20) -> Dict:
    """把缓存中热度最高的岗位加入待投递台账（幂等，按 job_id 去重）。"""
    tracker = load_tracker(tracker_path)
    existing = {a["job_id"] for a in tracker["applications"]}
    candidates = sorted(jobs, key=lambda j: (-j.get("heat", 0), j.get("title", "")))
    added = 0
    for job in candidates:
        job_id = job.get("job_id") or f"{job.get('company')}_{job.get('title')}"
        if job_id in existing:
            continue
        tracker["applications"].append({
            "job_id": job_id, "title": job.get("title", ""),
            "company": job.get("company", ""), "city": job.get("city", ""),
            "salary": job.get("salary", ""), "jd_url": job.get("url", ""),
            "status": "待投递", "resume_version": "", "note": "",
            "planned_at": now_iso(), "applied_at": "",
        })
        existing.add(job_id)
        added += 1
        if added >= limit:
            break
    return _save(tracker, tracker_path)


def update_status(tracker_path: str, job_id: str, status: str, note: str = "") -> Dict:
    if status not in STATUSES:
        raise ValueError(f"未知状态 {status}，可选: {STATUSES}")
    tracker = load_tracker(tracker_path)
    for application in tracker["applications"]:
        if application["job_id"] == job_id:
            application["status"] = status
            if note:
                application["note"] = note
            if status == "已投递":
                application["applied_at"] = now_iso()
            break
    else:
        raise KeyError(f"台账中无此岗位: {job_id}")
    return _save(tracker, tracker_path)


def add_invitation(tracker_path: str, company: str, title: str,
                   when: str = "", channel: str = "") -> Dict:
    tracker = load_tracker(tracker_path)
    tracker["invitations"].append({
        "company": company, "title": title, "when": when,
        "channel": channel, "created_at": now_iso(), "status": "待确认",
    })
    return _save(tracker, tracker_path)


def build_submission_packet(tracker_path: str, output_dir: str) -> Dict:
    """生成 dry-run 投递材料包（Markdown 清单 + JSON 明细），不外发。"""
    from pathlib import Path

    from core.cli import write_json

    tracker = load_tracker(tracker_path)
    pending: List[Dict] = [a for a in tracker["applications"] if a["status"] == "待投递"]
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    lines = [
        "# 投递材料包（dry-run，未自动提交）", "",
        f"生成时间：{now_iso()}  待投递：{len(pending)}", "",
        "| 公司 | 岗位 | 城市 | 薪资 | JD | 简历版本 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in pending:
        lines.append("| {company} | {title} | {city} | {salary} | {jd} | {resume} |".format(
            company=item["company"], title=item["title"], city=item["city"],
            salary=item["salary"], jd=item.get("jd_url", "") or "-",
            resume=item.get("resume_version") or "待定",
        ))
    lines += ["", "提交前请人工确认：岗位仍在招、简历版本匹配、投递渠道已授权。", ""]
    md_path = out / "submission_packet.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    write_json(out / "submission_packet.json", {"generated_at": now_iso(), "pending": pending})
    return {"pending": len(pending), "markdown": str(md_path)}


def summary(tracker_path: str) -> Dict:
    tracker = load_tracker(tracker_path)
    counts = {status: 0 for status in STATUSES}
    for item in tracker["applications"]:
        counts[item.get("status", "待投递")] = counts.get(item.get("status", "待投递"), 0) + 1
    return {"total": len(tracker["applications"]), "by_status": counts,
            "invitations": len(tracker["invitations"]), "updated": tracker.get("updated", "")}
