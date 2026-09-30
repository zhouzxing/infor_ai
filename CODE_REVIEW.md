# AI 情报聚合器 — 代码审阅地图

> 全部代码已集中在 `infor_ai_lab/` 目录下，无任何散落副本。
> 核心逻辑单文件 `ai_intel_aggregator.py`（126 KB / 1740 行），无第三方包依赖（标准库 + 可选 requests）。

---

## 1. 目录清单

| 文件 | 大小 | 说明 |
|---|---|---|
| `ai_intel_aggregator.py` | 126 KB | **唯一源码**，全部逻辑 |
| `index.html` | 659 KB | 生成物（5 个 tab，自动生成勿手改） |
| `ai_intel_cache.json` | 309 KB | 生成物（缓存：国际 100 / 国内 100 条） |
| `ai_daily_brief_20260923.md` | 10 KB | 历史简报，与代码无关 |
| `*.bak` / `__pycache__/` | - | 备份与字节码，可删 |

---

## 2. 代码结构地图（按行号）

### 配置区 (L18–31)
```
L22  MAX_ENTRIES = 300     ← 硬上限；≤100 全保留，>100 触发剔除
L23  MAX_DAYS    = 30      ← 保留窗口
L26  PROXY_HTTP  = "http://127.0.0.1:7897"   ← 必须配置，否则 403/超时
```

### 正文提取与清洗 (L33–249) ★ 摘要质量核心
```
L39   clean_html_to_text()      HTML → 纯文本
L56   _keep_sentence()          句子是否保留（阈值 8 字）
L63   _ARTICLE_CACHE            url → (summary, body)，同篇只抓一次
L64   _FETCH_DEADLINE           全局时间预算（420s），超时用 RSS 描述兜底
L66   _fetch_allowed()          时间预算门禁
L70   _clean_para()             单段落清洗
L74   _strip_page_chrome()      ★ 剥离 nav/header/footer/aside/script 等骨架
L96   _collect_paragraphs()     取段落块（先剥骨架）
L135  _best_para_block()        ★ 按密度打分选最密连续块，非盲取开头
L161  extract_article_body()    抓原文 → 开头段(2-3) + 结尾段(2-3) 拼接
L220  build_summary()           ★ RSS 描述 <50 字时降级抓原文
L238  build_body()
```
**5W1H 策略**：开头段覆盖 Who/What/Why/When/Where，结尾段覆盖 How/结论。

### 数据源注册表 + HTTP (L251–466)
```
L253  SOURCE_REGISTRY           ★ 38 个源（国际 26 + 国内 12），4 元组
L295  _http_get()               requests → urllib 双兜底
L328  fetch_rss_feed()
L385  fetch_all_sources()       并发抓取所有源
L406  record_source_status()    数据源活跃度（成功/失败/失联）
L426  cache_sources()           固化活跃度状态
```

### 专用抓取器 (L467–738) — 非 RSS 源
```
L468  fetch_from_geeker()       极客公园
L500  fetch_from_huxiu()        虎嗅（RSSHub）
L529  fetch_from_36kr()         36氪（RSSHub，偶发 0 items）
L558  fetch_from_ithome()       IT之家
L590  fetch_from_leifeng()      雷锋网
L619  fetch_from_infoq()        InfoQ 中文
L649  fetch_from_hackernews()   Hacker News
L678  fetch_from_google_news()  Google News ★ JS 跳转链接，见 §5 已知限制
L714  fetch_from_arxiv()
```

### 缓存管理 (L739–793)
```
L740  load_cache() / L748 save_cache()
L758  bump_heat()               重复出现 → 热度 +1
L770  clean_and_trim()          ★ 新规则：
                                 ≤100 条全保留
                                 >100 条剔除「3天外 + 热度≤1」
                                 >300 条按热度截取前 300
L790  sort_entries()            按 (-heat, timestamp) 排序
```

### 静态数据表 (L794–1005) — 硬编码
```
L796  MODEL_REGISTRY     88 个模型 / 23 家公司
L967  AGENT_PLATFORMS    29 个 Agent 平台（国际 16 + 国内 13），9 字段
```

### HTML 渲染 (L1006–1530)
```
L1007 render_html()        5 个 tab：国际情报 / 国内情报 / 国际 Agent / 国内 Agent / 模型对比
L1528 _esc()               HTML 转义
```

### 主流程 (L1531–1740)
```
L1532 migrate_cache()      旧缓存幂等迁移（清洗 HTML 残片、补链接）
L1582 seed_sources_history()
L1602 __main__             入口
  1603  load_cache()
  1608  migrate_cache()
  1612  判断是否需刷新（>1 小时才抓）
  1624  fetch_all_sources()           并发抓 RSS
  1635  ThreadPoolExecutor(24) 预抓   并行预抓所有新条目原文，预算 420s
  1649  第二轮重试                     串行重试完全失败的 URL
  1677  第三轮补抓                     串行补抓 <50 字短摘要
  1720  clean_and_trim()
  1722  sort_entries()
  1724  save_cache()
  1729  render_html() → index.html
```

---

## 3. 运行与部署

**手动运行**
```bash
cd /home/geeker/architecture/home/geeker/code_repo/best_practice_perf/labs_ai_/agent_app/infor_ai_lab
python3 ai_intel_aggregator.py
```

**定时任务（已配置，每小时整点）**
```
0 * * * * cd .../infor_ai_lab && python3 ai_intel_aggregator.py >> ~/.hermes/cache/scratch/ai_intel_cron.log 2>&1
```
日志：`~/.hermes/cache/scratch/ai_intel_cron.log`

**前置条件**
- 代理 `http://127.0.0.1:7897` 必须可达，否则大部分源 403 或超时
- Python ≥ 3.9；`requests` 可选（缺失时自动降级 urllib）

---

## 4. 关键设计决策

| 决策 | 原因 |
|---|---|
| 摘要用「开头段 + 结尾段」而非前 N 段 | 前 N 段常是导语+广告位；结尾段才有 How/结论，补齐 5W1H |
| 先剥页面骨架再取段落 | 否则 IEEE Spectrum 等站点导航文字污染摘要 |
| 最密段落块打分（总分 × log(段均长)） | 盲取开头会命中短碎导航；密度打分让长正文块自然胜出 |
| RSS 描述 <50 字就抓原文 | 门槛从「仅标签碎片时抓」提高，大幅提升摘要完整度 |
| 时间预算 420s + 24 并发 | 334 URL / 24 线程 ≈ 180s，留 240s 余量 |
| 三轮抓取（并发预抓 → 重试失败 → 补抓短摘要） | 应对代理并发限流导致的间歇性失败 |
| Google News 不绕过 | JS 跳转链接，HTTP 无法解析，已确认技术限制 |

---

## 5. 已知技术限制（接受，未绕过）

1. **Google News JS 跳转链接**（国内 14 条短摘要全部属于此类）
   URL 形如 `news.google.com/rss/articles/CBMiYEFV...`，HTTP 请求返回 JS 渲染页面，无法解析出真实 URL。protobuf 解码尝试失败（字段结构复杂且脆弱）。
   **可选解法**：接入无头浏览器（Playwright/Puppeteer）执行 JS 后再抓；或移除该源，改用各站独立 RSS。

2. **强反爬站点无法提取正文**：Ars Technica、Investing.com、Reddit（Cloudflare/WAF）。三轮重试全部失败，属正常现象。

3. **36氪 RSSHub**：公共实例不稳定，偶发 0 items。

4. **部分源 RSS 描述仅 7 字**（如「点击查看原文>」）：InfoQ 等源 RSS 不含摘要正文，必须抓原文页，已被 build_summary 的 <50 字降级逻辑覆盖。

---

## 6. 当前数据状态

| 指标 | 国际 | 国内 |
|---|---|---|
| 条目数 | 100 | 100 |
| 摘要平均字数 | 262 | ~180 |
| <50 字条目 | 0 | 14（全为 Google News） |

数据源活跃度：38 个源中，约 25 个活跃、13 个待同步（多为强反爬站点）。
