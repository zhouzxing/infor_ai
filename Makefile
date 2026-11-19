# AI 生态嗅探系统 · 自动化流水线
# 设计：五大模块各自独立 CLI；数据 / 实现分离；前后端靠 api/ 契约解耦。
# 所有命令从本目录执行：make <target>；可用 make -C <path> <target> 从外部调用。

PYTHON := python3
VENV   := .venv
CACHE  := output/cache/ai_intel_cache.json
INTEL  := output/intel
LOGDIR := output/logs
APIDIR := api
TRACKER:= output/jd/applications.json

.PHONY: help install data-validate fmt lint cache-init cache-clean status \
        fetch-news fetch-agent fetch-github fetch-arxiv fetch-jd fetch-all \
        analyze-news forecast rankings analyze-agent analyze-jd analyze-all \
        resume apply-plan apply-packet monitor build-api serve \
        test clean pipeline cron

# ─── 帮助 ──────────────────────────────────────────────────
help:
	@echo "AI 生态嗅探系统 —— 可用命令"
	@echo ""
	@echo "  环境/数据"
	@echo "    make install         创建虚拟环境并安装依赖（核心路径仅标准库）"
	@echo "    make data-validate   校验 data/*.json 数据层（42源/评分权重/主题文件）"
	@echo "    make cache-init      初始化缓存与日志目录"
	@echo "    make cache-clean     清空缓存与运行产物（保留目录）"
	@echo "    make status          查看缓存条目数 / 源状态摘要"
	@echo ""
	@echo "  ① 资讯网络"
	@echo "    make fetch-news      抓取 42 个 RSS/Atom 源（需代理）"
	@echo "    make analyze-news    热度/趋势/关键词 → intel_news.json"
	@echo "    make forecast        预判信号 + 敏感度 → intel_forecast.json"
	@echo "    make rankings        模型/Agent/开源四类榜单 → intel_rankings.json"
	@echo ""
	@echo "  ② Agent 实验室"
	@echo "    make fetch-agent     抓取 GitHub 热库 + arXiv 论文（需 gh 登录 / 代理）"
	@echo "    make fetch-github    fetch-agent 的别名"
	@echo "    make fetch-arxiv     fetch-agent 的别名"
	@echo "    make analyze-agent   商业/研发动态 → intel_agent.json"
	@echo ""
	@echo "  ③ JD 雷达"
	@echo "    make fetch-jd        智联 JD 抓取（反爬严格，可能 0 条）"
	@echo "    make analyze-jd      热门/高薪/主力方向 → intel_jd.json"
	@echo "    make resume          按 JD 优化 resume.md → resume_advice.json"
	@echo "    make apply-plan      生成投递计划（本地台账）"
	@echo "    make apply-packet    生成投递材料包（dry-run，不外发）"
	@echo ""
	@echo "  ④ 监控中心"
	@echo "    make monitor         数据源/反爬/日志 → monitor.json"
	@echo ""
	@echo "  ⑤ 大盘 + 全局"
	@echo "    make build-api       聚合所有产出 → api/data.js + contract.json"
	@echo "    make serve           本地预览 http://localhost:8080/frontend/index.html"
	@echo "    make analyze-all     仅运行全部分析（不抓网络）"
	@echo "    make pipeline        抓取 → 分析 → 监控 → 构建契约"
	@echo "    make cron            定时任务入口（等同 pipeline）"
	@echo "    make test            单元测试（unittest，无第三方依赖）"
	@echo "    make clean           清理所有产物"

# ─── 环境 ──────────────────────────────────────────────────
install:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt

data-validate:
	@$(PYTHON) -c "from core.data_loader import validate_data; e = validate_data(); print('数据层校验:', e or 'PASS'); raise SystemExit(1 if e else 0)"

fmt:
	@$(PYTHON) -m black core modules tests scripts 2>/dev/null || echo "跳过：未安装 black"
	@$(PYTHON) -m isort core modules tests scripts 2>/dev/null || echo "跳过：未安装 isort"

lint:
	@$(PYTHON) -m ruff check core modules tests scripts 2>/dev/null || echo "跳过：未安装 ruff"

# ─── 缓存 ──────────────────────────────────────────────────
cache-init:
	@mkdir -p output/cache output/intel output/logs output/jd api

cache-clean:
	@rm -f output/cache/*.json output/intel/*.json output/jd/*.json api/*.js api/*.json
	@rm -f output/logs/*.log
	@echo "✓ 缓存与分析产物已清空"

status:
	@$(PYTHON) scripts/status.py

# ─── 采集（写缓存，纯副作用集中在 collect.py）──────────────
fetch-news: cache-init
	@$(PYTHON) -m modules.intel_network.cli --action collect

fetch-agent: cache-init
	@$(PYTHON) -m modules.agent_lab.cli --action collect

fetch-github: fetch-agent

fetch-arxiv: fetch-agent

fetch-jd: cache-init
	@$(PYTHON) -m modules.jd_radar.cli --action collect

fetch-all: fetch-news fetch-agent fetch-jd
	@echo "✓ 四路采集完成（news / github+arxiv / jd，顺序执行便于排查）"

# ─── 分析（只读缓存 + data/*.json）─────────────────────────
analyze-news:
	@$(PYTHON) -m modules.intel_network.cli --action analyze

forecast:
	@$(PYTHON) -m modules.intel_network.cli --action forecast

rankings:
	@$(PYTHON) -m modules.intel_network.cli --action rankings

analyze-agent:
	@$(PYTHON) -m modules.agent_lab.cli --action analyze

analyze-jd:
	@$(PYTHON) -m modules.jd_radar.cli --action analyze

analyze-all: analyze-news forecast rankings analyze-agent analyze-jd
	@echo "✓ 全量分析完成"

resume:
	@$(PYTHON) -m modules.jd_radar.cli --action resume

apply-plan:
	@$(PYTHON) -m modules.jd_radar.cli --action apply --apply-action plan

apply-packet:
	@$(PYTHON) -m modules.jd_radar.cli --action apply --apply-action packet

# ─── 监控 / 大盘 ───────────────────────────────────────────
monitor:
	@$(PYTHON) -m modules.monitor_center.cli

build-api:
	@$(PYTHON) -m modules.dashboard.cli

serve: build-api
	@echo "🌐 预览：http://localhost:8080/frontend/index.html"
	@$(PYTHON) -m http.server 8080

test:
	@$(PYTHON) -m unittest discover -s tests -v

clean: cache-clean
	@rm -rf $(VENV) __pycache__ .pytest_cache .mypy_cache .ruff_cache
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "✓ 清理完成"

pipeline: data-validate fetch-all analyze-all monitor build-api
	@echo "✅ 完整流水线完成"

cron: pipeline
	@echo "⏰ Cron 任务完成"
