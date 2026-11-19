"""端到端离线路线测试：数据层 → 缓存 → 五大模块 → API 契约。

只用临时目录和标准库，不发任何网络请求。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import store
from core.cli import read_json
from core.data_loader import models, scoring, sources, validate_data
from core.feed import clean_html_to_text, extract_article_body, normalize_date, parse_feed
from core.http import classify_error
from core.schema import empty_cache, migrate_cache, validate_cache
from core.scoring import composite_score, parse_context, parse_params, parse_price, score_all_products
from core.store import CacheManager

from modules.agent_lab.analyze import analyze_agent
from modules.dashboard.aggregate import build_contract, write_contract
from modules.intel_network.analyze import analyze_news
from modules.intel_network.forecast import build_forecast, extract_signals
from modules.intel_network.rankings import build_rankings, oss_community_ranking
from modules.jd_radar.analyze import analyze_jd
from modules.jd_radar.apply import (add_invitation, build_submission_packet,
                                    plan_applications, summary, update_status)
from modules.jd_radar.resume import optimize_resume
from modules.monitor_center.aggregate import generate_monitor
from modules.monitor_center.source_health import build_channel_table

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>New agent model launched</title><link>https://example.com/a</link>
<description>Agent reasoning and long context new market.</description>
<pubDate>Tue, 18 Nov 2026 10:00:00 GMT</pubDate><category>AI</category></item>
</channel></rss>"""


class DataLayerTests(unittest.TestCase):
    def test_data_files_valid(self):
        self.assertEqual(validate_data(), [])
        self.assertEqual(len(sources()), 42)
        self.assertAlmostEqual(sum(scoring()["dim_weights"].values()), 1.0)

    def test_scoring_helpers(self):
        self.assertEqual(parse_context("1M"), 1_000_000)
        self.assertEqual(parse_params("685B (37B active, MoE)"), 37.0)
        self.assertEqual(parse_price("$0.28/$0.70"), 0.49)
        scored = score_all_products(models())
        self.assertGreater(len(scored), 50)
        for row in scored[:5]:
            self.assertGreaterEqual(composite_score(row["scores"]), 0)


class CoreTests(unittest.TestCase):
    def test_feed_parse_and_clean(self):
        items = parse_feed(RSS)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "New agent model launched")
        self.assertIn("2026", normalize_date("Tue, 18 Nov 2026 10:00:00 GMT"))
        text = clean_html_to_text("<script>bad()</script><p>hello &amp; world</p>")
        self.assertNotIn("bad()", text)
        self.assertIn("hello & world", text)
        summary, body = extract_article_body("<p>" + "正文内容较长，" * 20 + "</p>")
        self.assertTrue(summary and body)

    def test_error_classification(self):
        self.assertIn("反爬", classify_error("HTTP 403: Forbidden"))
        self.assertIn("代理", classify_error("URLError: timed out"))

    def test_legacy_status_migration(self):
        cache = empty_cache()
        cache["meta"]["sources_status"]["legacy"] = {"status": "活跃", "dot": "good"}
        migrate_cache(cache)
        status = cache["meta"]["sources_status"]["legacy"]
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["dot"], "green")

    def test_cache_dedup_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            store._MANAGERS.clear()
            cache = CacheManager(path)
            entry = {
                "title": "同一条资讯", "summary": "s", "body": "b", "url": "u",
                "sources": ["A"], "heat": 0, "timestamp": "2026-11-19T10:00:00",
                "date": "2026-11-19", "region": "int",
            }
            cache.add_entries([entry], "int")
            cache.add_entries([{**entry, "sources": ["B"]}], "int")
            stored = cache.get_entries("int")
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["heat"], 1)
            self.assertIn("B", stored[0]["sources"])
            self.assertEqual(validate_cache(cache.load()), [])


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        store._MANAGERS.clear()
        self.cache_path = str(self.root / "cache.json")
        self.intel_dir = self.root / "intel"
        self.intel_dir.mkdir()
        cache = CacheManager(self.cache_path)
        cache_data = cache.load()
        for i in range(3):
            cache_data["entries_int"].append({
                "title": f"新市场 Agent 推理 融资 {i}", "summary": "s", "body": "b",
                "url": f"u{i}", "sources": ["Google News"], "heat": 5,
                "timestamp": "2026-11-19T10:00:00", "date": "2026-11-19", "region": "int",
            })
        cache_data["github_repos"]["items"] = [{
            "full_name": "huggingface/transformers", "name": "transformers",
            "lang": "Python", "stars": 100, "forks": 10, "pushed": "2026-11-01",
            "rising": False, "org_pick": True, "desc": "LLM research transformer benchmark",
            "url": "https://github.com/huggingface/transformers", "topics": ["llm"],
        }]
        cache.save()

    def tearDown(self):
        self.tmp.cleanup()
        store._MANAGERS.clear()

    def test_news_forecast_rankings(self):
        news = analyze_news(self.cache_path, str(self.intel_dir / "intel_news.json"))
        self.assertEqual(news["total_entries"], 3)
        signals = extract_signals([{"title": "融资 新市场 Agent", "heat": 5,
                                    "date": "2026-11-19", "sources": ["x"]}])
        self.assertTrue(any(s["type"] == "新市场" for s in signals))
        build_forecast(news and CacheManager(self.cache_path).get_entries("int"),
                       str(self.intel_dir / "intel_forecast.json"))
        buckets = oss_community_ranking(CacheManager(self.cache_path).load()["github_repos"]["items"])
        self.assertIn("科研算法", buckets["buckets"])
        self.assertIn("教育培训", buckets["buckets"])
        build_rankings(self.cache_path, str(self.intel_dir / "intel_rankings.json"))
        self.assertGreater(read_json(self.intel_dir / "intel_rankings.json")["model_vendors"]["total_models"], 0)

    def test_jd_resume_apply(self):
        jobs = [{
            "job_id": "a", "title": "Agent 工程师", "company": "某公司", "city": "深圳",
            "salary": "40k-60k", "skills": ["LangChain", "RAG"], "heat": 12,
            "url": "https://example.com", "experience": "3-5年", "education": "本科",
            "description": "", "posted_date": "2026-11-19", "keyword": "Agent 开发",
            "is_hot": True, "is_big_company": False,
        }]
        advice = optimize_resume("熟悉 Python，做过 RAG 项目。", jobs)
        self.assertIn("RAG", advice["matched_skills"])
        self.assertIn("LangChain", advice["missing_skills"])
        CacheManager(self.cache_path).update_jd(jobs, ["Agent 开发"], ["深圳"], 1, 0, 1, [])
        jd = analyze_jd(self.cache_path, str(self.intel_dir / "intel_jd.json"))
        self.assertEqual(jd["total_jobs"], 1)

        tracker = str(self.root / "applications.json")
        plan_applications(jobs, tracker, 5)
        update_status(tracker, "a", "已投递", "已人工提交")
        add_invitation(tracker, "某公司", "Agent 工程师", "2026-11-25")
        packet = build_submission_packet(tracker, str(self.root / "jd"))
        self.assertEqual(packet["pending"], 0)
        self.assertEqual(summary(tracker)["invitations"], 1)
        self.assertTrue((self.root / "jd" / "submission_packet.md").exists())

    def test_agent_analysis_and_monitor(self):
        analyze_agent(self.cache_path, str(self.intel_dir / "intel_agent.json"))
        agent = read_json(self.intel_dir / "intel_agent.json")
        self.assertIn("business", agent)
        self.assertIn("rd", agent)
        channels = build_channel_table(CacheManager(self.cache_path).load())
        self.assertEqual(len(channels), 42)
        monitor = generate_monitor(
            self.cache_path, str(self.intel_dir / "monitor.json"), str(self.root / "logs")
        )
        self.assertEqual(monitor["kpi"]["total_sources"], 42)

    def test_dashboard_contract(self):
        for name in ("intel_news.json", "intel_forecast.json", "intel_rankings.json",
                     "intel_agent.json", "intel_jd.json", "monitor.json"):
            (self.intel_dir / name).write_text(json.dumps({}), encoding="utf-8")
        analyze_news(self.cache_path, str(self.intel_dir / "intel_news.json"))
        build_rankings(self.cache_path, str(self.intel_dir / "intel_rankings.json"))
        generate_monitor(self.cache_path, str(self.intel_dir / "monitor.json"), str(self.root / "logs"))
        contract = build_contract(self.cache_path, str(self.intel_dir), str(self.root / "applications.json"))
        self.assertEqual(len(contract["sections"]), 7)
        self.assertEqual(contract["contract_version"], "1.0")
        self.assertIn("kpi", contract)
        api_dir = self.root / "api"
        manifest = write_contract(contract, api_dir)
        self.assertEqual(len(manifest["files"]), 3)
        self.assertIn("window.INTEL_DATA", (api_dir / "data.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
