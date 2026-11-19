# 代码审查地图（infor_ai_trands）

> 目标：让审查者 10 分钟内定位改动影响面。按「职责 → 不变量 → 缺陷史 → 检查命令」组织。

---

## 1. 文件职责速查

| 想审查什么 | 先看哪个文件 |
| --- | --- |
| 缓存字段 / 迁移 / 裁剪 | `core/schema.py` → `core/store.py` |
| 网络重试 / 反爬归因 | `core/http.py: http_get / classify_error` |
| RSS 解析 / 正文提取 | `core/feed.py: parse_feed / fetch_rss_feed / extract_article_body` |
| 模型评分 | `core/scoring.py: score_product / score_all_products` + `data/scoring.json` |
| 数据是否漏配 | `core/data_loader.py: validate_data` + `make data-validate` |
| ① 资讯抓取 / 预判 / 四类榜单 | `modules/intel_network/{collect,forecast,rankings}.py` |
| ② GitHub / arXiv / 商业研发动态 | `modules/agent_lab/{collect,analyze}.py` + `data/company_dynamics.json` |
| ③ JD / 简历 / 投递台账 | `modules/jd_radar/{collect,analyze,resume,apply}.py` |
| ④ 数据源 / 反爬 / 日志 | `modules/monitor_center/{source_health,crawler_health,log_pipeline,aggregate}.py` |
| ⑤ 大盘区块 / 契约字段 | `modules/dashboard/aggregate.py: build_contract` |
| 前端渲染 / XSS 转义 | `frontend/app.js: esc / table / render*` |
| 自动化入口 | `Makefile` + 各模块 `cli.py` |

---

## 2. 审查不变量（违反即打回）

1. **依赖方向**：`data ← core ← modules ← dashboard ← frontend`。`core` 不得 import `modules`；`modules` 不得互相 import（共用逻辑放 `core`）。
2. **写权限**：只有 `*/collect.py` 和 `modules/jd_radar/apply.py`（本地台账）能写 `output/cache`；`analyze.py` 只读。
3. **数据外置**：`data/*.json` 是唯一静态数据来源；Python 文件里不得出现源 URL / 平台大表 / 权重字面量。
4. **原子写**：所有 JSON 落盘走 `core/cli.py: write_json`（`.tmp` + `replace`）。
5. **前端安全**：`app.js` 任何来自契约的文本都要过 `esc()`；外链带 `rel="noopener"`。
6. **可解释性**：预判 / 诊断输出必须带证据字段（关键词、命中数、原文），不得给出无来源结论。
7. **dry-run 边界**：`apply.py` 不得引入任何对外提交 / 邮件 / IM 发送行为；投递必须人工确认。
8. **测试覆盖**：新增分析函数要在 `tests/test_pipeline.py` 加一条离线用例。

---

## 3. 从 0.1 修复的缺陷（避免回归）

| 0.1 问题 | 本版处理 | 对应位置 |
| --- | --- | --- |
| `re.split(r"[\s\p{P}]+")` 在 Python re 下抛异常 | 改为 CJK/英文词元正则 | `modules/intel_network/analyze.py:_TOKEN_RE` |
| `fetch_rss_feed` 引用未定义的 `title` 变量 | 用 `item.title` | `core/feed.py:fetch_rss_feed` |
| `Tuple/Optional`、`strip_html`、`datetime` 写在文件末尾导入 | 全部改为顶部导入 | `core/feed.py`、`modules/jd_radar/collect.py` |
| `get_cache` 单例忽略后续 cache_path | 按路径维护 `_MANAGERS` | `core/store.py:get_cache` |
| 监控连败次数靠 status 猜 3/99 | 从 `sources_history` 真实倒推 | `modules/monitor_center/source_health.py:_consecutive_failures` |
| `clean_html_to_text` 里 `replace("&","&")` 空操作 | 改用 `html.unescape` | `core/feed.py:clean_html_to_text` |
| 0.1 的 `_parse_salary` 注解用到未导入的 `Optional/Tuple` | 本版 `parse_salary` 用 `from __future__ import annotations` + 顶部导入 | `modules/jd_radar/collect.py:parse_salary` |
| 渲染单文件 756 行，CSS/JS/视图混在一起 | 前端独立为 html/css/js + 3 主题 | `frontend/` |
| 文档声称 89 模型 / 58 平台，实际数据 64 / 56 | 以 `data/*.json` 为准并校核 | `core/data_loader.py:validate_data` |

---

## 4. 审查命令

```bash
make data-validate                       # 数据层门禁
make test                                # 10 项离线单测
node --check frontend/app.js             # 前端语法
node --check api/data.js                 # 契约语法
make analyze-all monitor build-api       # 离线全链路（不联网）
make status                              # 缓存 / 源 / 契约摘要
```

审查改动时的最小闭环：改 JSON → `make data-validate`；改 Python → `make test`；改前端 → `node --check` + `make build-api` 打开 `frontend/index.html` 目视三主题切换。
