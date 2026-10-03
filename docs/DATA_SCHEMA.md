# 数据契约 — `ai_intel_cache.json` 字段级 schema

> 单一事实源。抓取层写、渲染层读。**改任何字段前先读这里 + 对照 `migrate_cache`。**
> 版本：v7（GitHub 热榜 tab 并入）

---

## 1. 顶层

| 键 | 类型 | 说明 | 写入者 |
|---|---|---|---|
| `entries_int` | `entry[]` | 国际资讯，≤300，按 `(-heat, timestamp)` 排序 | 抓取 |
| `entries_cn` | `entry[]` | 国内资讯，≤300，同序 | 抓取 |
| `meta` | object | 元信息 + 数据源状态 | 抓取 |
| `github_repos` | object | GitHub 热榜（3h TTL） | 抓取 |

### meta
| 键 | 类型 | 说明 |
|---|---|---|
| `updated` | ISO8601 | 本次渲染时间戳 |
| `last_source_refresh` | ISO8601 | 最近一次 `cache_sources` 固化时间 |
| `sources_history` | `{源名: hist_ev[]}` | 每源保留最近 48 条抓取结果 |
| `sources_status` | `{源名: status_obj}` | 由 history 派生的当前活跃度面板数据 |

`hist_ev`：`{ "t": ISO, "n": int(条目数), "ok": bool, "err": str(≤120) }`

`status_obj`：
```jsonc
{ "region":"int|cn","url":str,"total_items":int,
  "status":"活跃|重试中|失联|待同步", "dot":"good|bad|muted",
  "last_ok":bool,"last_ok_t":ISO,"last_err":str,"stale":bool,
  "history_ok":int,"history_total":int }
```
判定：`last_ok && !stale`→活跃；`!last_ok && last_err`→重试中；`stale(>24h)`→失联；否则→待同步。

---

## 2. 资讯 `entry`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `title` | str | ✓ | 已 strip |
| `summary` | str | ✓ | 清洗后的纯文本摘要（HTML 残片已由 `migrate_cache` 清掉） |
| `body` | str | ○ | 正文（开头段+结尾段拼接，5W1H） |
| `url` | str | ✓ | 原文链接；缺失时 `migrate_cache` 从 summary 的 `href=` 反推 |
| `sources` | `str[]` | ✓ | 命中该条的所有源名（多源累加热度的依据） |
| `heat` | int | ✓ | 0–5，`bump_heat` cap 5；论文/官方大厂发布初始 3，其余 2 |
| `timestamp` | ISO | ✓ | 入库时间 |
| `date` | `YYYY-MM-DD` | ✓ | 归日期 |

---

## 3. GitHub `github_repos`

```jsonc
{ "items": gh_item[], "orgs": ["owner_login", …], "updated": ISO, "errors": ["…", …] }
```

`errors` 非空表示某路抓取失败（3h 内 TTL 命中时直接复用旧数据，不再抓）。

### gh_item
| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 仓库短名 |
| `full_name` | str | `owner/repo` |
| `owner` | str | owner login |
| `owner_type` | `"org"\|"user"` | 渲染徽章「组织/个人」依据 |
| `lang` | str | 编程语言，缺失为 `"—"` |
| `stars` | int | star 数 |
| `forks` | int | fork 数 |
| `created` | `YYYY-MM-DD` | 创建日期 |
| `pushed` | `YYYY-MM-DD` | 最近 push |
| `rising` | bool | `created >= 2026-06-01`（近 4 月新星） |
| `org_pick` | bool | 是否命中 8 个精选组织的 top10（渲染「精选」绿标） |
| `desc` | str | 描述，截断 160 字 |
| `url` | str | `https://github.com/owner/repo` |
| `topics` | `str[]` | 前 5 个 topic tag |

> 噪音过滤：`_gh_is_ai` 对「仅来自宽泛 topic 榜」的仓库做 AI 关键词二次筛（命中
> `llm/ai/agent/gpt/model/neural/ml` 之一才留）；来自组织精选/新星的直接保留。

---

## 4. 静态大表（不进缓存，`ai_intel_aggregator.py` 字面量）

### MODEL_REGISTRY — 88 模型 / 23 公司
按**公司**分组，每个产品：`region(int|cn), product, params, context, modalities, license, pricing, free, docs, enterprise, limitations, features, release`。
渲染层六维打分 `DIM_W = {ctx:.18, mm:.18, open:.16, cost:.22, scale:.12, fresh:.14}`。

### AGENT_PLATFORMS — 29 平台
每个平台：`region, product, category, pricing, free, models, features, api, docs, ecosystem, enterprise, limitations`。

> 这两张表**人工维护**。增删字段会连带渲染层表格列，需同步改 `ai_intel_render.py`。

---

## 5. Schema 演进规则

1. **加字段**：新字段给默认值，`load_cache` 对旧缓存补默认，渲染层用 `.get(k, 默认)` 读。
2. **改语义 / 删字段**：在 `migrate_cache` 加分支做**幂等**迁移（已迁的不再动）。
3. **破坏性变更**：加 `meta.schema_version`，`migrate_cache` 按版本路由。
4. **测试**：迁移函数写完后用旧 `.bak` 缓存跑一遍 `migrate_cache` 验证幂等。

---

## 6. 抓取层常量（`ai_intel_aggregator.py` 顶部）

| 常量 | 值 | 含义 |
|---|---|---|
| `MAX_ENTRIES` | 300 | 每区上限；>300 按热度截 |
| `MAX_DAYS` | 30 | 资讯保留窗口 |
| `PROXY_HTTP` | `http://127.0.0.1:7897` | **必须**，无代理全 403/超时 |
| `PROXY_SOCKS` | `socks5://…` | 备用 |
| GitHub TTL | 10800s | `load_github_cache` 3h 命中即不重抓 |
