# AI 情报聚合 — 数据契约与 Schema

> 面向对象：接入方、二次开发者。
> 版本：v8（42 源 / 58 Agent / 89 模型；删除 v1 遗留 fetch_from_* 死代码）
> 最近更新：2026-10-06

---

## 0. 总览

| 数据资产 | 位置 | 生产者 | 消费者 |
|---|---|---|---|
| `ai_intel_cache.json` | 项目根 | `ai_intel_aggregator.py::save_cache` | `ai_intel_render.py::render_page` |
| `MODEL_REGISTRY` | `ai_intel_aggregator.py` 模块级字面量 | 人工维护 | 渲染层「模型库」「能力大盘」 |
| `AGENT_PLATFORMS` | `ai_intel_aggregator.py` 模块级字面量 | 人工维护（v8: 58 平台） | 渲染层「Agent 平台」「能力大盘」 |
| `SOURCE_REGISTRY` | `ai_intel_aggregator.py` 模块级字面量 | 人工维护（v8: 42 源） | 抓取 + 渲染监控 |
| `index.html` | 项目根（交付物） | `ai_intel_render.py` | 浏览器（离线可开） |

---

## 1. `ai_intel_cache.json`（单一事实源）

### 1.1 顶层结构

```jsonc
{
  "entries_int": [ Entry, ... ],     // 国际资讯，≤300 条
  "entries_cn":  [ Entry, ... ],     // 国内资讯，≤300 条
  "meta": {
    "updated": "2026-10-06T22:35:04.123",  // 上次渲染时间 ISO8601
    "last_source_refresh": "ISO8601",       // 上次源健康度刷新
    "schema_version": "v8",                 // 可选，v8 起新增
    "sources_history": { ... },             // §1.5
    "sources_status":  { ... }              // §1.6
  },
  "github_repos": {                     // §1.7
    "items": [ GhItem, ... ],           // ≤150 条
    "orgs":  [ "openai", "anthropics", ... ],
    "updated": "ISO8601",
    "errors": [ "..." ]
  }
}
```

### 1.2 `Entry`（资讯条目）

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | string | 清洗后标题（去噪、去换行） |
| `summary` | string | 50–260 字摘要；正文提取失败时降级到 RSS description |
| `body` | string | 500–2000 字正文；开头段(2-3) + 结尾段(2-3) 拼接 |
| `url` | string | 原文 URL（`google.com/rss/articles/…` 属 Google News 跳转，无法直连原文） |
| `sources` | string[] | 命中源列表；多源重复 +1 到 heat |
| `heat` | int | 0–5，`bump_heat` 累加（多源 +1，cap 5） |
| `timestamp` | string ISO8601 | RSS `published/updated` 或抓取时间 |
| `date` | string YYYY-MM-DD | `timestamp` 的前 10 位 |

**质量保障**：`migrate_cache` 会幂等清洗旧缓存里 `summary/body` 的 HTML 残片（`<section`/`<div` 开头等）。

**裁剪规则**（`clean_and_trim`）：
1. 时间窗 `MAX_DAYS=30`
2. 每区 `MAX_ENTRIES=300`
3. ≤100 条全保留；>100 条剔除「3 天外 + heat≤1」
4. >300 按 `(-heat, -timestamp)` 截前 300

### 1.3 `Model`（模型条目，来自 `MODEL_REGISTRY[cid]["products"]`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 全局唯一，建议格式 `{cid}:{slug}`，如 `anthropic:claude-4-5-sonnet` |
| `name` | string | 展示名 |
| `code` | string | 内部代号（可空） |
| `date` | string YYYY-MM | 首发年月 |
| `ctx` | int | 上下文长度（token 数；≥1000000 表示 1M+） |
| `mm` | string[] | 模态列表，取值见 §2 |
| `open` | bool | 是否开源 |
| `cost_i` | float | 输入价格，$/1M tokens（0 = 免费） |
| `cost_o` | float | 输出价格，$/1M tokens |
| `scale` | int | 参数量（int；`scale_t: true` 表示单位为 T 而非 B） |
| `scale_t` | bool | 参数量单位标志 |
| `fresh` | int | 2026 年内迭代代数（1..N） |
| `tags` | string[] | 标签，如 `agent`/`reasoning`/`safety`/`realtime` |

**维度打分**（`score_product` in aggregator）：
- `ctx`、`mm`、`open`、`cost`、`scale`、`fresh` 六维各 0–100
- 权重 `DIM_W = {ctx:.18, mm:.18, open:.16, cost:.22, scale:.12, fresh:.14}`
- `composite = Σ w_i × s_i`

### 1.4 `AgentPlatform`（Agent 平台条目，来自 `AGENT_PLATFORMS`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `region` | "int" \| "cn" | 国际 / 国内 |
| `product` | string | 产品名；厂商归属可括注，如 `"AutoGen (Microsoft Research)"` |
| `category` | string | 见 §3 分类枚举 |
| `pricing` | string | 计费方式，如 `"按Token计费"` / `"订阅制"` / `"开源免费"` |
| `free` | string | `"有"` / `"有(有限)"` / `"有限"` / `"无"` |
| `models` | string | 支持模型列表（逗号分隔） |
| `features` | string | 核心能力（逗号分隔） |
| `api` | string | API/SDK 名 |
| `docs` | string | 官方文档 URL |
| `ecosystem` | string | 生态描述 |
| `enterprise` | string | 企业版名称（无则填 `"无"`） |
| `limitations` | string | 已知限制 |

**当前库规模**：58 平台 / 6 大分类（详见 §3）

### 1.5 `meta.sources_history`

```jsonc
{
  "<source_name>": [
    { "t": "ISO8601", "n": 42, "ok": true, "err": "" },
    ...                       // 每源保留最近 48 条
  ]
}
```
- `n`：本轮抓取条目数
- `ok`：本轮是否成功
- `err`：错误摘要（成功时空串）

### 1.6 `meta.sources_status`（渲染层直接读，无需重算）

```jsonc
{
  "<source_name>": {
    "name":       "String",
    "region":     "int" | "cn",
    "url":        "HTTP URL",
    "total_items": int,       // 历史累计条目数
    "status":     "active" | "retrying" | "lost" | "pending",
    "dot":        "green" | "yellow" | "red" | "gray",
    "last_ok_t":  "ISO8601" | null,
    "last_err":   "error summary" | "",
    "stale":      bool,       // >24h 未成功即失联
    "history_ok": int,
    "history_total": int
  }
}
```

**状态判定**（`cache_sources` in aggregator）：
- `active` (green)：最近一次成功
- `retrying` (yellow)：最近一次失败但历史曾成功
- `lost` (red)：`>24h` 未成功（`stale=True`）
- `pending` (gray)：从未抓取

### 1.7 `github_repos.items[]`（`GhItem`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | repo 短名 |
| `full_name` | string | `owner/repo` |
| `owner` | string | 用户名 |
| `owner_type` | "org" \| "user" | 决定徽章类型 |
| `lang` | string \| null | 主语言 |
| `stars` | int | 星数 |
| `forks` | int | fork 数 |
| `created` | string YYYY-MM-DD | 建仓日期 |
| `pushed` | string YYYY-MM-DD | 最近 push |
| `rising` | bool | 是否新星（近 4 月建仓） |
| `org_pick` | bool | 是否组织精选 |
| `desc` | string | README 简介 |
| `url` | string | 完整 GitHub URL |
| `topics` | string[] | GitHub topic |

**去重键**：`full_name`

**筛选机制**（`fetch_github_repos`）：
1. 主题榜 `topic:llm/ai/ai-agents/genai/generative-ai` → `_gh_is_ai` 二次过滤（避免 `topic:ai` 混入 `n8n`/水印工具）
2. 新星：`created:>2026-06-01`
3. 组织精选：9 个组织各取 top10

**TTL**：3 小时（`load_github_cache`）。抓取失败保留旧数据 + 记 `errors`，不影响其他 tab。

---

## 2. 枚举与约定

### 2.1 模态 `mm[]` 取值
`text` / `image` / `audio` / `video` / `speech` / `code` / `tool`

### 2.2 区域 `region`
- `int`：国际
- `cn`：国内

### 2.3 源类型（`fetch_rss_feed`）
- RSS 2.0 / Atom / JSON feed 均支持
- `<item>` / `<entry>` 双结构

### 2.4 协议徽章（渲染层 `.mfmt`）
- `rss2` (green)：RSS 2.0
- `atom` (purple)：Atom
- `unknown` (gray)：无法识别

---

## 3. `AGENT_PLATFORMS` 分类枚举（v8: 58 平台）

| Category | 代表条目 |
|---|---|
| 通用Agent | OpenAI、Anthropic、Google DeepMind、商汤日日新、DeepSeek、智谱、月之暗面、腾讯元器 |
| 开源Agent | LangChain、CrewAI、Hermes Agent、Agno、SmolAgents、BabyAGI、Camel-AI |
| LLM应用平台 | LangChain、LlamaIndex、Dify、Dify (中国版) |
| Agent 编排 | Temporal、Zapier Agents、Make AI、AgentUniverse |
| 编程Agent | GitHub Copilot、Amazon Q Developer、Aider、Cline、Continue、Pieces、Roo Code、Cursor、Windsurf、Sourcegraph Cody |
| 企业Agent平台 | Replit |
| IDE/工作流 | Cursor、Windsurf、Hugging Face |
| Agent 框架 | Semantic Kernel、Swarm |
| 通用平台 (微软/亚马逊) | Microsoft、Amazon |
| 企业Agent | 华为云盘古、字节跳动、阿里巴巴、百度 |
| 智能设备Agent | 小米超级小爱、荣耀魔法大模型 |
| 创业Agent | 阶跃星辰、MiniMax |
| 开源Agent平台 / 知识库Agent | Dify、FastGPT、MaxKB、LangBot、百川 |
| 行业Agent | 科大讯飞星火 |

> 分类字段是自由文本，不做严格枚举。计数以 `len(AGENT_PLATFORMS)` 为准。

---

## 4. `SOURCE_REGISTRY` 结构（v8: 42 源）

```python
SOURCE_REGISTRY = [
    (name: str,       # 展示名，同时是 meta.sources_history 的键
     region: str,     # "int" | "cn"
     url: str,        # RSS/Atom/JSON feed URL（国内源多为 rsshub 镜像）
     keywords: list,  # 空列表 = 垂直 AI 频道不过滤；非空 = 需命中任一
    ),
    ...
]
```

**v8 新增 4 源**：
| 源名 | 区域 | URL | 过滤 |
|---|---|---|---|
| Reddit r/LocalLLaMA | int | https://www.reddit.com/r/LocalLLaMA/.rss | 空（垂直频道） |
| Hacker News (hnrss) | int | https://hnrss.org/newest?q=AI+OR+LLM+OR+GPT+OR+agent+OR+model+OR+OpenAI+OR+Claude | 空 |
| GeekerHub (RSS) | cn | https://www.geekerhub.com/feed | `["AI","人工智能","大模型","OpenAI","ChatGPT","机器学习","算法","开发者","效率","编程","工具","开源","GitHub"]` |
| arXiv CS.AI | int | https://rss.arxiv.org/rss/cs.AI | 学术 AI 关键词 |

**v8 清理**：删除 9 个 v1 遗留 `fetch_from_*` 函数（`geeker/huxiu/36kr/ithome/leifeng/infoq/hackernews/google_news/arxiv`）。它们的主流程早不调用，导致监控面板与实际抓取路径漂移；删除后监控面板完全对齐实际数据源。

---

## 5. `meta.run_log`（可选，渲染监控中心用）

`list[str]`，每轮 cron 追加一行摘要，保留最近 48 条。
格式示例：`"2026-10-06 22:35 | int=187 cn=64 | GH=143 | 12/12 sources ok"`

跳过抓取轮：`"2026-10-06 22:00 | skipped (no update needed)"`

---

## 6. `index.html` 契约

- **完全自包含**：内联 CSS（约 60KB）+ JS（约 35KB），无外链
- **无任何运行时网络请求**：所有数据在生成时写死进 HTML
- **六个 tab**：`news / dash / models / agents / github / monitor`
- **通用表格机制**：`data-table-search` 绑搜索框，`data-table-filter` 绑 region 三段按钮，行上 `data-region` 分类
- **GitHub 榜复用**：行上 `data-region="org"/"user"` 即接入通用过滤，零新增 JS

---

## 7. 迁移与兼容

### 7.1 `migrate_cache`（幂等）
1. 清洗 `entry.summary/body` 的 HTML 残片（`<section`/`<div`/`<nav` 等开头）
2. 补全缺失 `url`（用 `entry.sources` 反查）
3. 补全缺失 `date`（从 `timestamp` 前 10 位）

### 7.2 `seed_sources_history`
对从未记过状态的旧缓存，用 `sources_status.total_items` 反推一次历史，让监控面板立即显示合理数据。

### 7.3 Schema 演进
- 加字段：新字段给默认值即可，旧缓存自动兼容
- 删字段：先在 `migrate_cache` 做降级
- 大改：加 `meta.schema_version`，`migrate_cache` 按版本增量迁移

---

## 8. 快速校验脚本

```python
import json
from pathlib import Path

cache = json.loads(Path('ai_intel_cache.json').read_text(encoding='utf-8'))

assert set(cache) >= {"entries_int","entries_cn","meta"}
assert "github_repos" in cache

for region in ("entries_int","entries_cn"):
    for e in cache[region]:
        assert set(e) >= {"title","summary","url","sources","heat","timestamp"}
        assert 0 <= e["heat"] <= 5

status = cache["meta"].get("sources_status", {})
assert len(status) >= 30  # v8: 42 源，允许早期缓存未跑满

if cache.get("github_repos", {}).get("items"):
    for g in cache["github_repos"]["items"]:
        assert g["owner_type"] in ("org","user")
        assert isinstance(g["stars"], int)

print("schema OK")
```

---

## 9. 字段变更日志

| 版本 | 时间 | 变更 |
|---|---|---|
| v8 | 2026-10-06 | SOURCE_REGISTRY 38→42 源；AGENT_PLATFORMS 29→58；删除 9 个 v1 遗留 fetch_from_* 死函数 |
| v7 | 2026-10-05 | 新增 `meta.run_log`、GitHub 榜三源合并、`_gh_is_ai` 二次过滤 |
| v6 | 2026-10-04 | 新增 `github_repos`；渲染层加 GitHub 榜 tab |
| v5 | 2026-10-03 | `entry.body` 引入开头段+结尾段拼接 |
| v4 | 2026-10-02 | `meta.sources_status` 固化，渲染层不再重算 |
| v3 | 2026-10-01 | 引入 `bump_heat` 多源累加 |
| v2 | 2026-09-30 | `migrate_cache` 幂等清洗 HTML 残片 |
| v1 | 2026-09-28 | 初版 |
