# AI 情报聚合 — 架构设计与实现文档

> 面向对象：后期维护者、架构升级者。
> 版本：v8（Agent 库扩展到 58 平台；数据源扩展到 42；清理 v1 遗留 fetch_from_* 死代码）
> 代码目录：`infor_ai_lab/`
> 最近更新：2026-10-06

---

## 0. 一句话概述

一个**零依赖的静态站点生成器**：`ai_intel_aggregator.py` 按小时从 **42 个 RSS/Atom 源**（国际 30 + 国内 12）抓取 AI 资讯，并从 GitHub Search API 抓热榜 → 落盘到 `ai_intel_cache.json`（单一事实源）→ `ai_intel_render.py` 读缓存 + 两张静态大表，注入到内联了全部 CSS/JS 的**自包含 `index.html`**。产物无任何运行时网络请求，可离线双击打开，也可丢进任意静态托管。

三个可执行入口：
- `python3 ai_intel_aggregator.py` — 抓取 + 渲染全流程（cron 调用）
- `ai_intel_render.render_page(cache, ...)` — 纯渲染（测试 / 复用）
- `index.html` — 最终产物，唯一交付物

---

## 1. 目录与文件

| 文件 | 角色 | 是否可手改 | 说明 |
|---|---|---|---|
| `ai_intel_aggregator.py` | **抓取 + 编排**（主逻辑） | 可 | RSS/HTML/GitHub 抓取、缓存读写、编排、模型/Agent 静态大表 |
| `ai_intel_render.py` | **渲染** | 可 | 读缓存+静态表 → 生成 HTML/CSS/JS 自包含页 |
| `ai_intel_cache.json` | **单一事实源** | 生成物 | 资讯 + 数据源状态 + GitHub 热榜；渲染只读它 |
| `index.html` | **交付物**（自包含） | 生成物，勿手改 | 内联 CSS+JS，离线可开 |
| `MODEL_REGISTRY` | 静态数据（在 aggregator 内） | 可 | 89 模型 / 23 公司，人工维护 |
| `AGENT_PLATFORMS` | 静态数据（在 aggregator 内） | 可 | **58 Agent 平台**，人工维护 |
| `CODE_REVIEW.md` | 代码审阅地图（历史） | 参考 | 按行号索引旧版布局，行号已漂移，仅读结构参考 |
| `ai_daily_brief_*.md` | 历史简报 | 无关 | 与代码解耦 |
| `*.bak` / `__pycache__/` | 备份 / 字节码 | 可删 | — |

> 关键约定：**缓存是唯一事实源**。抓取层写缓存，渲染层只读缓存。二者之间没有运行时依赖，改任一侧不会破坏另一侧的产物。

---

## 2. 数据流总览

```
                    ┌─────────────────────────────────────────────┐
   42 个 RSS 源       │            ai_intel_aggregator.py           │
  ┌──────────┐      │                                               │
  │RSS/Atom │──────▶│  fetch_all_sources()  并发 10 线程            │
  │42 源     │      │        │                                      │
  ├──────────┤      │        ▼  命中缓存/420s 时间预算               │
  │Hacker   │      │  extract_article_body()  正文预抓(并发)+重试     │
  │News/     │──────▶  build_summary/build_body  摘要质量清洗        │
  │arXiv     │      │        │                                      │
  └──────────┘      │        ▼                                      │
  ┌──────────┐      │  clean_and_trim + sort_entries  热度/时间排序   │
  │GitHub    │      │        │  (heat: 多源累加, ≤100全保留, >300截)  │
  │search API│──────▶  fetch_github_repos()  主题榜+新星+组织精选     │
  └──────────┘      │        │                                      │
                    │        ▼  record/cache_sources  数据源活跃度     │
                    │   save_cache(cache) ────────────┐             │
                    └─────────────────────────────────┼─────────────┘
                                                      ▼
                                            ai_intel_cache.json
                          { entries_int, entries_cn, meta, github_repos }
                                                      │
                                                      ▼  render_page(cache, ...)
                    ┌─────────────────────────────────┴─────────────┐
                    │            ai_intel_render.py                 │
                    │  资讯流 + 能力大盘 + 模型库 + Agent库 + GitHub榜 + 监控中心 │
                    │  + 内联 CSS/JS + 6 个 nav tab                  │
                    └─────────────────────────────────┬─────────────┘
                                                      ▼
                                            index.html (自包含, 离线)
```

---

## 3. 抓取层（`ai_intel_aggregator.py`）

### 3.1 数据源注册表 `SOURCE_REGISTRY`（~L253）
四元组：`(名称, 区域, URL, AI关键词过滤)`。
- 共 **42 个源：国际 30 + 国内 12**（v8 新增：Reddit r/LocalLLaMA、Hacker News (hnrss)、GeekerHub、arXiv CS.AI）。
- `keywords=[]` 表示该源是垂直 AI 频道，不做关键词过滤；非空则要求条目 title/description 命中任一关键词才收录。
- 国内源大量走 `rsshub.rssforever.com` 镜像（虎嗅/36氪无官方 RSS）。
- **v8 清理**：v1 遗留的 `fetch_from_*` 函数（geeker/huxiu/36kr/ithome/leifeng/infoq/hackernews/google_news/arxiv 共 9 个）已删除。它们的职能已由 `SOURCE_REGISTRY` + `fetch_rss_feed` 统一接管，主流程早就不调用它们。删除后所有源监控统计与实际调用路径一致。

### 3.2 统一 HTTP `_http_get`（~L295）
requests → urllib **双兜底**；必须配代理 `PROXY_HTTP = http://127.0.0.1:7897`（模块 import 时即写进 `os.environ`）。**没有代理会 403 / 超时**——这是本项目最常见的环境故障。

### 3.3 正文提取与清洗（★ 摘要质量核心，~L39–249）
RSS 的 `description` 常带整段 HTML，直接截断会得到 `"<section style=..."` 残片。处理链：
1. `clean_html_to_text` — HTML → 纯文本
2. `_strip_page_chrome` — 剥离 nav/header/footer/aside/script/svg 等页面骨架（否则 IEEE Spectrum 这类站点的 `<p>` 塞满导航项）
3. `_collect_paragraphs` + `_best_para_block` — **按文字密度打分选最密连续块**，非盲取开头
4. `extract_article_body` — 开头段(2-3) + 结尾段(2-3) 拼接，覆盖 5W1H
5. `build_summary` / `build_body` — 摘要 <50 字时**降级抓原文**兜底

**性能兜底**：`_ARTICLE_CACHE`（url→(summary,body)，同篇只抓一次）+ `_FETCH_DEADLINE`（420s 全局时间预算，超时用 RSS 描述兜底）。main 里正文抓取是**并发预抓(24线程) → 串行重试 → 短摘要补抓**三轮。

### 3.4 并发抓取 `fetch_all_sources`（~L385）
`ThreadPoolExecutor(10)` 抓全部 42 源，返回 `(by_region, errors)`。

### 3.5 数据源活跃度面板（~L406–466）
`record_source_status` 成功/失败都记进 `meta.sources_history`（每源保留最近 48 条）；`cache_sources` 固化成 `meta.sources_status`，渲染面板据此显示 活跃/重试中/失联/待同步 四态（>24h 未成功 = 失联）。

### 3.6 GitHub 热榜 `fetch_github_repos`（~L527）
走 `gh api`（需 `gh auth` 登录 + 代理）。三路抓取合并去重：
1. **主题榜**：逐 `topic:` 拉 `topic:llm/ai/ai-agents/genai/generative-ai` 的 stars 榜（`topic:ai` 过宽，需 `_gh_is_ai` 关键词二次筛除 `n8n`/水印工具这类噪音）
2. **新星**：`created:>2026-06-01`（近 4 月创建）
3. **组织精选**：`openai/anthropics/facebook/google/mistralai/deepseek-ai/huggingface/NousResearch` 各取 top10

归一化字段见 §5.3。**3 小时 TTL**（`load_github_cache`），抓取失败只打印 WARN 并保留旧数据，**不影响其他 4 个 tab**。耗时 ~42s。

### 3.7 去重 / 热度 / 裁剪
- `slug` / `already_exists` — 标题归一化去重
- `bump_heat` — 多源重复出现，热度 +1（cap 5）
- `clean_and_trim` — **≤100 条全保留；>100 条剔除「3 天外 + 热度≤1」；>300 按热度截前 300**
- `sort_entries` — 按 `(-heat, timestamp)` 排序

### 3.8 静态大表
- `MODEL_REGISTRY`（~L655）— 89 模型 / 23 公司，人工维护
- `AGENT_PLATFORMS`（~L826）— **58 平台**，人工维护。v8 扩展后覆盖：
  - **国际 Agent 平台/云服务**（16 个）：OpenAI、Anthropic、Google DeepMind、Microsoft、Amazon、Cohere、Databricks、LangChain、LlamaIndex、CrewAI、n8n、Dify、Replit、Cursor、Windsurf、Hugging Face
  - **国际开源 Agent 框架**（9 个）：Hermes Agent、AutoGen、Semantic Kernel、Swarm、SmolAgents、BabyAGI、Agno、AgentUniverse、Camel-AI
  - **编程 Agent**（10 个）：GitHub Copilot、Amazon Q Developer、Sourcegraph Cody、Aider、Cline、Continue、Pieces、Roo Code、Cursor、Windsurf
  - **Agent 编排/工作流**（3 个）：Temporal、Zapier Agents、Make AI
  - **国内通用/企业 Agent**（11 个）：字节跳动、阿里巴巴、百度、商汤、DeepSeek、智谱、月之暗面、百川、面壁智能、华为云、腾讯元器、科大讯飞星火
  - **国内开源 Agent 平台**（3 个）：Dify (中国版)、FastGPT、MaxKB、LangBot
  - **国内设备/助手 Agent**（2 个）：小米超级小爱、荣耀魔法大模型
  - **国内创业 Agent**（2 个）：阶跃星辰、MiniMax

---

## 4. 渲染层（`ai_intel_render.py`）

### 4.1 入口 `render_page(cache, model_registry, agent_platforms, max_entries, max_days)`（L189）
纯函数：输入缓存 + 两张静态表 → 输出完整 HTML 字符串。无副作用，便于单测。

### 4.2 六个导航 tab（`nav` ~L425）
| key | 标签 | 数据来源 |
|---|---|---|
| `news` | 资讯聚合 | `cache.entries_int/cn` |
| `dash` | 能力大盘 | `MODEL_REGISTRY` + `AGENT_PLATFORMS` 统计 |
| `models` | 模型库 | `MODEL_REGISTRY` |
| `agents` | Agent 平台 | `AGENT_PLATFORMS` |
| `github` | GitHub 热榜 ★ | `cache.github_repos` |
| `monitor` | 监控中心 ★ | `cache.meta.*`（sources_status / sources_history / run_log） |

### 4.3 六维能力打分（模型库）
`DIM_W = {ctx:.18, mm:.18, open:.16, cost:.22, scale:.12, fresh:.14}`，`score_product` 逐维归一 0–100，`composite` 加权求综合分。渲染为迷你柱状条 + 雷达图（`_radar_svg`）。

### 4.4 前端交互（内联 `JS`）
- 视图切换 `showView`（tab 互斥）
- 全局模糊检索（子串 + 子序列打分）
- **通用表格过滤机制**（复用）：`[data-table-search]` 绑搜索框，`[data-table-filter]` 绑三段式 region 按钮，行上设 `data-region`。GitHub 榜设 `data-region="org"/"user"` 即白嫖这套机制，**零新增 JS**。

### 4.5 GitHub 榜视图（`github_view` ~L631）
4 张 KPI（收录/新星/组织数/语言数）+ 9 列表格（#、项目、类型、语言、Stars、Forks、最近更新、简介、tags）+ 组织/个人三段过滤 + 模糊搜索。语言用品牌色圆点，类型用「组织/个人」徽章，`org_pick` 仓库挂「精选」绿标、`rising` 挂「新星」橙标。

### 4.6 zhouzxing 宣传位（3 处，内联 octicon）
顶栏常驻徽章 / GitHub 页作者 CTA 卡 / 页脚署名，均链向 `github.com/zhouzxing`。改头像/账号只需全局替换该 URL。

### 4.7 监控中心视图（`view-monitor` ~L760）
三层结构，全部只读 `cache.meta`，无运行时网络请求：
- **资讯监控 KPI**：资讯总量（国际/国内分列）、24h 新增（按 entry.timestamp 窗口）、渠道活跃 x/42、连败+失联渠道数。
- **四面板**（`monitor-grid`）：
  - 资讯热度分布：heat 0–5 六档横向条（`cmp-row`）。
  - 来源贡献 TOP10：按 entry.sources 去重计数取前十，横向条。
  - 爬取技术栈：7 张说明卡（HTTP 双兜底 / RSS-Atom 双格式 / 关键词过滤 / 正文三轮抓取 / 热度累加 / GitHub API / 渠道稳定性自身），状态点取自各子系统健康度。
  - 管线运行日志：`meta.run_log`（每轮一行，保留 48 条）显示最近 12 条；跳过轮标「跳过抓取」灰签。
- **渠道稳定性表**：42 行 × 8 列（渠道/区域/爬取策略/协议/成功率/连败/状态/最近错误），复用 `[data-table-search="mon-src-table"]` 通用模糊检索（行上 `data-search` 只含名/策略/格式/错误，**不含 URL**——镜像源 URL 普遍带 rsshub，混入会让检索命中全表）。协议徽章 `.mfmt`：rss2 绿 / atom 紫 / unknown 灰。

> **v8 修复**：`monitor` 显示的活跃源清单与 `SOURCE_REGISTRY` 现在**完全一致**。此前的 v1 遗留 `fetch_from_*` 函数（如 Hacker News 走 firebaseio API、arXiv CS.AI、GeekerHub）主流程不调用，导致监控面板显示的源与实际抓取的源存在漂移。删除这批死代码后，监控面板 100% 反映真实抓取路径。

---

## 5. 数据契约（`ai_intel_cache.json`）

> 字段级细节见 `docs/DATA_SCHEMA.md`。这里是结构骨架。

### 5.1 顶层
```jsonc
{
  "entries_int": [ entry, ... ],   // 国际资讯, ≤300, 按 (-heat, ts) 排序
  "entries_cn":  [ entry, ... ],   // 国内资讯, ≤300
  "meta": {
    "updated": "ISO8601",          // 渲染时间戳
    "sources_history": { 源名: [{t,n,ok,err}, …(≤48)] },
    "sources_status":  { 源名: {region,url,total_items,status,dot,last_ok_t,last_err,stale,history_ok,history_total} },
    "last_source_refresh": "ISO8601"
  },
  "github_repos": { items:[gh_item], orgs:[…], updated:"ISO8601", errors:[…] }
}
```

### 5.2 资讯 `entry`
```jsonc
{ "title","summary","body","url","sources":[源名],"heat":0-5,"timestamp":"ISO","date":"YYYY-MM-DD" }
```

### 5.3 GitHub `gh_item`
```jsonc
{ "name","full_name","owner","owner_type":"org"|"user","lang","stars":int,
  "forks":int,"created":"YYYY-MM-DD","pushed":"YYYY-MM-DD",
  "rising":bool,"org_pick":bool,"desc","url","topics":[…] }
```

### 5.4 迁移兜底
`migrate_cache` 幂等清洗旧缓存里的 HTML 残片 + 补全缺失 url；`seed_sources_history` 对从没记过状态的旧缓存反推一次。改缓存 schema 前先加迁移函数。

---

## 6. 部署与运行

### 6.1 前置
- Python 3.12：`/home/geeker/.asdf/installs/python/3.12.0/bin/python3`
- **代理**：`http://127.0.0.1:7897`（无代理抓取全挂）
- GitHub 榜额外需：`gh` CLI 已 `gh auth login`（当前 zhouzxing）

### 6.2 定时任务（已在 crontab）
```
0 * * * * cd …/infor_ai_lab && python3 ai_intel_aggregator.py \
          >> /home/geeker/.hermes/cache/scratch/ai_intel_cron.log 2>&1
```
每小时整点。单次耗时：资讯刷新 ~7min（正文三轮抓）+ GitHub ~42s。

### 6.3 手动
```bash
python3 ai_intel_aggregator.py        # 全流程
python3 -c "from ai_intel_aggregator import load_cache,MODEL_REGISTRY,AGENT_PLATFORMS;from ai_intel_render import render_page;open('index.html','w').write(render_page(load_cache(),MODEL_REGISTRY,AGENT_PLATFORMS))"  # 仅重渲染(不抓)
```

---

## 7. 维护指南

### 7.1 常见故障
| 症状 | 根因 | 处理 |
|---|---|---|
| 全源 0 条 / 403 / 超时 | 代理 `127.0.0.1:7897` 没起 | 起代理后重跑 |
| 页面黑屏（Three.js 类页面） | `file://` 拦 ES module CORS | 本项目**无此问题**（纯静态内联）；仅 Persona Core 那类 WebGL 页需注意 |
| GitHub 榜为空但其他 tab 正常 | `gh` 未登录 / 代理失效 | `gh auth status`；GitHub 榜有 3h TTL，不影响已生成的 4 tab |
| 摘要开头是 `<section`/`<div` 残片 | 旧缓存未迁移 | `migrate_cache` 已幂等清洗，重跑一次即净 |
| 某源显示「失联」 | >24h 未成功（源下线/cron 停） | 看 `meta.sources_status[name].last_err` 定位 |

### 7.2 改数据源
- 加/删源：只动 `SOURCE_REGISTRY` 四元组；渲染层不用改。
- 加 AI 关键词：改对应源的 `keywords` 列表。
- 国内无官方 RSS 的源走 `rsshub.rssforever.com` 镜像。
- **不要**再写 `fetch_from_xxx` 专用函数——全部走 `fetch_rss_feed`，否则监控面板会与实际调用路径脱节。

### 7.3 加模型 / Agent
改 `MODEL_REGISTRY` / `AGENT_PLATFORMS` 字面量。字段增删会连带渲染层表格列，需同步 `ai_intel_render.py` 的表头与行生成。

**Agent 平台字段规范**（AGENT_PLATFORMS 每个平台的字典键）：
```
region       : "int" | "cn"
product      : 产品名（含括号注释厂商归属，如 "AutoGen (Microsoft Research)"）
category     : "通用Agent" | "开源Agent" | "编程Agent" | "LLM应用平台" | "Agent 编排" | "企业Agent平台" | ...
pricing      : "按Token计费" | "订阅制" | "开源免费" | "免费/内置" | ...
free         : "有" | "有(有限)" | "有限"
models       : 支持模型列表（逗号分隔）
features     : 核心能力（逗号分隔）
api          : API 名/入口
docs         : 官方文档 URL
ecosystem    : 生态（社区/集成/平台）
enterprise   : 企业版名称
limitations  : 已知限制
```

**已收录 58 个 Agent 平台分类**（截至 v8）：
- 国际云服务 Agent（10）：OpenAI、Anthropic、Google DeepMind、Microsoft、Amazon、Cohere、Databricks、LangChain、LlamaIndex、CrewAI
- 国际开源 Agent 框架（9）：Hermes Agent、AutoGen、Semantic Kernel、Swarm、SmolAgents、BabyAGI、Agno、AgentUniverse、Camel-AI
- 国际编程 Agent（8）：GitHub Copilot、Amazon Q Developer、Sourcegraph Cody、Aider、Cline、Continue、Pieces、Roo Code
- 国际 IDE/工作流 Agent（4）：Cursor、Windsurf、Hugging Face、n8n
- 国际 LLM 应用平台（2）：Dify、Replit
- 国际 Agent 编排/工作流（3）：Temporal、Zapier Agents、Make AI
- 国内通用/企业 Agent（11）：字节跳动、阿里巴巴、百度、商汤、DeepSeek、智谱、月之暗面、百川、面壁智能、华为云、腾讯元器
- 国内创业 Agent（2）：阶跃星辰、MiniMax
- 国内智能设备 Agent（2）：小米超级小爱、荣耀魔法大模型
- 国内 LLM 应用/知识库平台（3）：Dify (中国版)、FastGPT、MaxKB
- 国内 Agent 机器人（1）：LangBot
- 国内厂商 Agent 补充（3）：科大讯飞星火、商汤日日新、百度文心

> 分类计数可能因条目归属调整有 ±1 浮动，以 `len(AGENT_PLATFORMS)` 为准。

### 7.4 加 tab / 视图
1. `nav` 列表加 `(key, 标签)`
2. 写一个 `<section class="view" id="view-{key}">`
3. 组装处（`html = … + news_view + … + agents_view + github_view`）拼进
4. 表格类复用 `data-table-search`/`data-table-filter`；卡片类参考 `news_view`

### 7.5 宣传位账号
全局搜 `github.com/zhouzxing` 替换即可（顶栏/作者卡/页脚 3 处 + 内联 SVG）。

---

## 8. 升级路径（非破坏性演进备忘）

1. **抽取数据层**：目前 `MODEL_REGISTRY`/`AGENT_PLATFORMS` 硬编码在 `.py`。升级可外置为 `models.json`/`agents.json`，抓取时合并进缓存，降低改静态表需重编译 Python 的摩擦。
2. **渲染与抓取解耦为服务**：当前 cron 产物靠静态托管分发。若要多端实时，可将 `index.html` 换成读 `ai_intel_cache.json` 的薄 SPA + 一个只读 API；但**离线自包含**是本项目卖点，改动需权衡。
3. **GitHub 榜分页/筛选**：现 150 条一次渲染。数据涨后可引入前端虚拟滚动或按「组织」分组折叠。
4. **正文抓取**：现为三轮（并发/重试/补抓）。可升级为按源质量加权 + 摘要去重嵌入向量，减少长文误抓。
5. **缓存 schema 版本**：加 `meta.schema_version`，`migrate_cache` 按版本做增量迁移，保护升级时历史数据。

---

## 9. 已知边界
- Google News 条目是 JS 跳转链接，点开仍可达，但无法直连原文（`fetch_rss_feed` 走 RSS 版本无法解开 Google News 跳转）。
- `36氪` 偶发 0 items（RSSHub 镜像抖动）。
- 国内源强依赖 `rsshub.rssforever.com` 可用性。
- 资讯保留窗口 `MAX_DAYS=30`、每区 `MAX_ENTRIES=300`，超长需调 `clean_and_trim`。
- `AGENT_PLATFORMS` 中 `Cursor`/`Windsurf` 同时出现两次（分类为「编程Agent」和「IDE/工作流」）——为保持原有分类可读性保留，如需去重请人工合并。

---

## 10. v8 变更记录（2026-10-06）

1. **AGENT_PLATFORMS 扩展到 58 平台**（原 29，+29）
   - 补国际开源 Agent 框架：Hermes Agent、AutoGen、Semantic Kernel、Swarm、SmolAgents、BabyAGI、Agno、AgentUniverse、Camel-AI
   - 补编程 Agent：GitHub Copilot、Amazon Q Developer、Sourcegraph Cody、Aider、Cline、Continue、Pieces、Roo Code
   - 补 Agent 编排：Temporal、Zapier Agents、Make AI
   - 补国内开源 Agent：FastGPT、MaxKB、LangBot
   - 补国内厂商：腾讯元器、科大讯飞星火、小米超级小爱、荣耀魔法大模型、阶跃星辰、MiniMax
2. **SOURCE_REGISTRY 扩展到 42 源**（原 38，+4）
   - 新增：Reddit r/LocalLLaMA、Hacker News (hnrss)、GeekerHub、arXiv CS.AI
3. **清理 v1 遗留死代码**：删除 `fetch_from_geeker / huxiu / 36kr / ithome / leifeng / infoq / hackernews / google_news / arxiv` 共 9 个函数（~271 行），主流程早已不调用，全部走 `fetch_rss_feed`
4. **修复监控面板与实际抓取路径漂移**：v1 遗留函数在主流程外被引用但从不实际调用，导致监控面板显示的活跃源与实际抓取源不一致；v8 删除后完全对齐
5. **文档同步更新**：本文件 + `docs/DATA_SCHEMA.md`
