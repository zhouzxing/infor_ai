"""模块③ CLI：抓取 / 分析 / 简历优化 / 投递台账。

示例：
  python3 -m modules.jd_radar.cli --action all
  python3 -m modules.jd_radar.cli --action resume --resume-file resume.md
  python3 -m modules.jd_radar.cli --action apply --apply-action plan
"""

from __future__ import annotations

import argparse

from core.cli import base_parser, output_dir, setup_logging, project_path
from core.store import get_cache

from .analyze import analyze_jd
from .apply import (add_invitation, build_submission_packet, plan_applications,
                    summary, update_status)
from .collect import fetch_jd
from .resume import optimize_resume

log = setup_logging(__name__)


def main() -> None:
    parser = base_parser("JD 雷达：抓取 / 热门高薪分析 / 简历优化 / 投递邀约台账")
    parser.add_argument("--action", default="all",
                        choices=["collect", "analyze", "resume", "apply", "all"])
    parser.add_argument("--data", default=str(output_dir("intel", "intel_jd.json")))
    parser.add_argument("--resume-file", default=str(project_path("resume.md")))
    parser.add_argument("--tracker", default=str(output_dir("jd", "applications.json")))
    parser.add_argument("--packet-dir", default=str(output_dir("jd")))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply-action", default="plan",
                        choices=["plan", "packet", "status", "invite", "summary"])
    parser.add_argument("--job-id")
    parser.add_argument("--status")
    parser.add_argument("--company")
    parser.add_argument("--title")
    parser.add_argument("--when", default="")
    parser.add_argument("--channel", default="")
    args = parser.parse_args()

    if args.action in ("collect", "all"):
        result = fetch_jd(args.cache)
        print(f"JD 抓取: {result['jobs']} 岗位, {len(result['errors'])} 错误")
    if args.action in ("analyze", "all"):
        data = analyze_jd(args.cache, args.data)
        print(f"JD 分析: {data['total_jobs']} 岗位")
    if args.action in ("resume", "all"):
        jobs = get_cache(args.cache).load().get("jd_jobs", {}).get("items", [])
        try:
            with open(args.resume_file, "r", encoding="utf-8") as fh:
                resume_text = fh.read()
        except FileNotFoundError:
            resume_text = ""
            print(f"提示：未找到 {args.resume_file}，使用空简历（仅输出目标技能栈）")
        result = optimize_resume(resume_text, jobs[:200])
        from core.cli import write_json

        write_json(output_dir("intel", "resume_advice.json"), result)
        print(f"简历匹配: 覆盖 {result['coverage']:.0%}, 缺口 {len(result['missing_skills'])} 项")
    if args.action in ("apply", "all"):
        jobs = get_cache(args.cache).load().get("jd_jobs", {}).get("items", [])
        if args.apply_action == "plan":
            result = plan_applications(jobs, args.tracker, args.limit)
            print(f"投递计划: {len(result['applications'])} 条")
        elif args.apply_action == "packet":
            result = build_submission_packet(args.tracker, args.packet_dir)
            print(f"材料包: {result['pending']} 条待投递（dry-run，未外发）")
        elif args.apply_action == "status":
            update_status(args.tracker, args.job_id or "", args.status or "已投递")
            print("状态已更新")
        elif args.apply_action == "invite":
            add_invitation(args.tracker, args.company or "", args.title or "",
                           args.when, args.channel)
            print("邀约已记录")
        else:
            print(summary(args.tracker))
    log.info("模块③ %s 完成", args.action)


if __name__ == "__main__":
    main()
