# AI 生态嗅探系统 · 架构设计与实现

> 面向：后期维护者、代码审查者、架构升级者
> 代码目录：`infor_ai_trands/`　契约版本：`1.0`　缓存 Schema：`v10`
> 最近更新：2026-11-19

---

## 0. 设计目标与落地方式

| 目标 | 落地方式 | 验证入口 |
| --- | --- | --- |
| 数据与实现分离 | 所有静态表外置为 `data/*.json`；代码只经 `core/data_loader.py` 读取，无任何大表字面量 | `make data-validate` |
| 前后端解耦 | 后端只产出 `api/data.js` / `contract.json` / `manifest.json`；前端是纯静态 HTML/CSS/JS，不含 Python 模板 | `make build-api` + `make serve` |
| Makefile 自动化 | 每个模块一个 CLI，Makefile 按「采集 → 分析 → 监控 → 契约」编排 | `make help` / `make pipeline` |
| 方便代码审查 | 五模块各自 `collect / analyze / cli`；函数短、副作用集中在采集层；单测覆盖数据→契约全链路 | `make test`（10 项） |

---

## 1. 分层架构

```
        ┌──────────────────────────────────────────────────────────┐
 L4     │ frontend/  纯静态：index.html + app.js + styles.css + 3 主题 │
        │ 只读 window.INTEL_DATA，不含模板、不发后端请求               │
        └───────────────────────▲──────────────────────────────────┘
                                │ api/data.js（唯一前后端接口）
        ┌───────────────────────┴──────────────────────────────────┐
 L3     │ modules/dashboard/aggregate.py：build_contract / write_contract │
        │ 把五大模块产出压成 7 个展示区块                              │
        └───────────────────────▲──────────────────────────────────┘
                                │ 只读 output/intel/*.json + output/jd/applications.json
        ┌───────────────────────┴──────────────────────────────────┐
 L2     │ ① intel_network  ② agent_lab  ③ jd_radar  ④ monitor_center │
        │ collect.py（唯一写缓存） / analyze.py（只读） / cli.py        │
        └───────────────────────▲──────────────────────────────────┘
                                │ 依赖
        ┌───────────────────────┴──────────────────────────────────┐
 L1     │ core/: schema · store · http · feed · scoring · data_loader · cli │
        │ 不含业务逻辑，不 import modules/*                           │
        └───────────────────────▲──────────────────────────────────┘
                                │ 加载
        ┌───────────────────────┴──────────────────────────────────┐
 L0     │ data/*.json：源 / 模型 / Agent / GitHub / arXiv / JD / 评分 / 主题 │
        └──────────────────────────────────────────────────────────┘
```

**依赖方向严格单向**：上层可依赖下层，下层不得反向 import。`core` 与 `modules` 之间只有 `core → modules` 一个方向。

---

## 2. 目录与文件职责

| 路径 | 职责 | 可手改 |
| --- | --- | --- |
| `data/sources.json` | 42 个资讯源四元组（name / region / url / keywords），国际 30 + 国内 12 | 是 |
| `data/models.json` | 25 厂商 / 64 产品的静态注册表（上下文、模态、定价、许可、发布时间） | 是 |
| `data/agents.json` | 56 个 Agent 平台（分类、计费、免费层、特性、企业版） | 是 |
| `data/github.json` | GitHub topics / orgs / rising_since | 是 |
| `data/arxiv_sources.json` / `arxiv_directions.json` | 5 个 arXiv 订阅 / 8 个研究方向关键词表 | 是 |
| `data/jd_keywords.json` | 9 个 JD 关键词 / 5 城市 / 31 个 AI 相关性词 | 是 |
| `data/scoring.json` | 六维权重、预判信号词、开源四分类词表、阈值、停用词 | 是 |
| `data/company_dynamics.json` | 12 条商业动态示例（`verified=false`，需二次核验） | 是 |
| `data/themes.json` | 3 种大盘主题 + 7 个展示区块定义 | 是 |
| `core/schema.py` | `SCHEMA_VERSION="v10"`；dataclass、空缓存、迁移、裁剪、校验 | 谨慎 |
| `core/store.py` | `CacheManager` 读写缓存；`get_cache(path)` 按路径维护实例 | 谨慎 |
| `core/http.py` | `http_get` 代理/重试；`classify_error` 反爬归因 | 是 |
| `core/feed.py` | RSS 2.0 / Atom / RDF 解析、正文提取（默认纯标准库） | 是 |
| `core/scoring.py` | 六维打分与解析工具，被 ① 和 ② 共用 | 是 |
| `core/data_loader.py` | `load_data` + 具名访问器 + `validate_data` | 是 |
| `modules/*/collect.py` | 唯一允许写缓存的位置 | 是 |
| `modules/*/analyze.py` | 只读缓存 + `data/`，输出结构化洞察 | 是 |
| `modules/*/cli.py` | Makefile 调用入口 | 是 |
| `api/data.js` | 前端唯一数据源：`window.INTEL_DATA = {...}` | 生成物 |
| `frontend/app.js` | 7 个区块的 DOM 渲染、tab/主题切换、转义 | 是 |
| `output/` | 缓存、分析产出、JD 台账、日志 | 生成物 |

---

## 3. 数据与实现分离

1. **静态数据全部 JSON 化**：0.1 版本把 60KB 数据写在 `config/__init__.py`；本版本把它拆成 9 个 JSON，`load_data(name)` 带 `lru_cache`，具名访问器如 `sources()` / `models()` / `agent_platforms()` 返回只读语义的缓存对象（调用方不得原地修改）。
2. **运行数据是单一事实源**：`output/cache/ai_intel_cache.json`（`core/schema.py` 定义字段）。采集层写，分析层读；分析层之间互不写对方文件。
3. **调参不改代码**：评分权重 `dim_weights`、预判词表 `signal_keywords`、开源分类词表 `oss_categories`、阈值 `thresholds` 全部在 `data/scoring.json`。例如把 cost 权重从 0.22 调高，只需改 JSON 并重跑 `make rankings build-api`。
4. **校验门禁**：`validate_data()` 检查 42 源 / 国际 30 / 国内 12、权重和为 1、必需评分字段、主题 CSS 是否存在。`make pipeline` 第一步就是它。

> 数据核对结果：当前实际数据为 **42 源 / 25 厂商 / 64 产品 / 56 Agent 平台 / 8 arXiv 方向**。0.1 文档写的「89 模型 / 58 平台」是历史文案，与数据文件不符，以 `data/*.json` 为准。

---

## 4. 前后端解耦

**契约**：`api/data.js` 中一行 `window.INTEL_DATA = {...}`，结构与 `api/contract.json` 完全一致；`manifest.json` 记版本、生成时间、7 个区块名与模块产出清单。

- `frontend/index.html` 只包含结构与容器；`app.js` 从全局对象取数并渲染；没有 f-string、没有后端模板、没有运行时网络请求。
- 离线可用：`file://` 直接打开 `frontend/index.html`（`<script src="../api/data.js">` 走相对路径），不受 fetch 的 CORS 限制；`make serve` 在根起 HTTP 服务，路径为 `/frontend/index.html`。
- 替换成 REST 的路径：把 `data.js` 换成 `fetch('/api/contract.json')`，`app.js` 的渲染函数无需改动；后端只需用任意 Web 框架暴露同一份 `contract.json`。
- 安全：`app.js` 的 `esc()` 对所有注入 DOM 的文本做 HTML 转义（含 `& < > " '`），外链加 `rel="noopener"`。

---

## 5. 数据流与运行

```
  RSS/Atom(42) ─①fetch_all──┐
  gh CLI + arXiv ─②fetch_all┼──▶ output/cache/ai_intel_cache.json（单一事实源）
  智联 JD ───────③fetch_jd──┘              │
                                           ├─①analyze_news → intel_news.json
  源状态/错误 ────④record_source_status─────┤─①build_forecast → intel_forecast.json
  管线日志 ──────④append_run_log────────────┤─①build_rankings → intel_rankings.json
                                           ├─②analyze_agent → intel_agent.json
                                           ├─③analyze_jd → intel_jd.json
                                           ├─④generate_monitor → monitor.json
                                           └─⑤build_contract → api/data.js + contract.json
```

- `CacheManager.add_entries` 以标题归一化（小写、去空白、截 80 字）去重；同一条被多源收录时 `heat +1`（cap 5）。
- `trim_entries` 按 30 天窗口过滤；≤100 条全保留，>100 条剔除「3 天外且 heat≤1」，最多 300 条/区。
- 所有写盘经 `write_json` 的 `.tmp → replace` 原子写。
- `get_cache(path)` 用路径作 key 维护实例，避免 0.1 版本「第一次调用后忽略新路径」的问题；测试里 `store._MANAGERS.clear()` 可重置。

---

## 6. 五大模块实现

### ① 资讯网络 `modules/intel_network/`

- **收集** `collect.py:fetch_all`：10 线程并发，全局 420s 预算；逐源 `record_source_status`；单源异常不影响其他源；结束写 `run_log`。`fetch_single` 供调试单源。
- **分析** `analyze.py:analyze_news`：热度分布、来源贡献、7 日趋势、关键词频次。`_TOKEN_RE = r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9.+-]{1,}"` 做中英混排分词（替代 0.1 中 Python 不支持的 `\p{P}`）。
- **预判** `forecast.py`：`extract_signals` 从高热条目按四类词表命中并去重；`build_forecast` 用「计数 ≥ 阈值」规则产出 `predictions`，并算行业 / 产业 / 政策敏感度（0~1）。`method` 字段明示是 `rule_based`，不是 LLM 推断。
- **排行榜** `rankings.py`：
  - `model_vendor_ranking()` → 厂商 API/线上服务/产品能力六维均分 + 代表模型；
  - `agent_vendor_ranking()` → 平台按特性数排序 + 分类/免费层分布；
  - `oss_community_ranking()` → GitHub 仓库按 `data/scoring.json` 的 `oss_categories` 词表命中数归入「科研算法 / 工程落地 / homelab / 教育培训 / 其他」，桶内按 stars 降序。

### ② Agent 实验室 `modules/agent_lab/`

- `collect.py:fetch_github`：`gh api` 三路合并（主题榜 / 新星 `created:>2026-06-01` / 20 个组织精选），`is_ai_repo` 用 `github_ai_keywords` 二次过滤噪音，按 `full_name` 去重，标记 `rising` / `org_pick`。
- `collect.py:fetch_arxiv`：5 个子类 RSS → `parse_feed` → `classify_direction`（8 方向关键词命中）→ `calculate_heat`（方向命中 + 新鲜度 + 标题高价值词，cap 20）→ base_id 去重 → 写缓存。
- `analyze.py:analyze_agent`：商业动态读 `company_dynamics.json`（估值 / IPO / 营收 / 产品线 / 收费变动 / 研发动态），研发动态读 GitHub 与 arXiv 缓存，输出 `business` + `rd` 两棵树。收费变动目前是**定价快照**，历史 diff 需接入快照库。

### ③ JD 雷达 `modules/jd_radar/`

- `collect.py`：`fetch_jd` 遍历 9 关键词 × 5 城市，`fetch_keyword_city` 带 0.5s 礼貌间隔；`parse_salary` 解析 `20k-40k` 等区间；`heat_score` 综合关键词命中 / 一线城市 / 大厂 / 高薪；AI 相关性用 31 词表过滤。
- `analyze.py:analyze_jd`：热门（heat≥10）、高薪（≥50k）、城市/技能/薪资段/经验学历分布、主力方向 top10。
- `resume.py:optimize_resume`：把目标 JD 技能频次作为 required，与 `resume.md` 文本做大小写不敏感匹配，输出覆盖率、缺口与 bullet 改写建议；明确禁止虚构经历。
- `apply.py`：本地台账（`STATUSES = 待投递/已投递/笔试/面试/offer/拒绝`），`plan_applications` 幂等去重，`update_status` 流转，`add_invitation` 邀约管理，`build_submission_packet` 生成 **dry-run** 材料包。**不自动向外部站点提交**——那需要账号授权且是对外可见行为；扩展点在下文。

### ④ 监控中心 `modules/monitor_center/`

- `source_health.py:build_channel_table`：42 行渠道表；`_consecutive_failures` 从 `sources_history` 真实倒推连败次数（0.1 是靠状态猜 3/99）；协议识别与镜像源分层。
- `crawler_health.py:diagnose`：`core/http.py:classify_error` 把 403/429/超时/解析失败归因，按原因计数并给改进建议（UA/Cookie、降并发、检查代理、更新解析规则）。
- `log_pipeline.py:_tail`：bounded-memory 尾部读取日志，避免把 2MB cron 日志整文件载入；`collect_logs` 汇总 `run_log` 与日志文件审计。
- `aggregate.py:generate_monitor`：KPI + 渠道 + 诊断 + 日志 + `source_value_assessment`（免费镜像 vs 官方源、失联清单、商业价值缺口说明）。

### ⑤ 大盘汇报 `modules/dashboard/`

`build_contract` 把产出压成 7 区块：`kpi`（一眼总览）、`opportunities`（投资/市场机会）、`alerts`（预警与敏感度）、`industry`（行业/技术动向）、`talent`（岗位人才）、`rankings`（三类榜单）、`monitor`（监控）。`write_contract` 同时写 JS / JSON / manifest 三份文件。前端按 `data/themes.json` 渲染 3 种样式：政务大盘（深蓝指标卡）、看板娘（明亮卡片）、预警大盘（深色告警色阶）。

---

## 7. 核心算法

**六维能力打分**（`core/scoring.py`）：`ctx` 上下文长度分档（≥2M=100 … <32K=20）；`mm` 模态覆盖（text 30 起，vision +20，audio +15，video +15，code/tool 各 +10）；`open` 许可证（Apache/MIT=100，community/CC=60，含 open=40，闭源=20）；`cost`（免费=100，≤$1=80，≤$5=60，≤$15=40，更高=20）；`scale`（MoE 取激活参数）；`fresh`（2026 年内越晚发布越高）。综合分：

```
composite = ctx*0.18 + mm*0.18 + open*0.16 + cost*0.22 + scale*0.12 + fresh*0.14
```

**热度累加**：多源重复出现 +1，上限 5（`CacheManager.add_entries`）。

**预判**：信号类型 4 类；计数阈值 `新市场≥3 / 产品形态≥3 / 功能特性≥3 / 行业敏感度≥2` 触发规则预判；`confidence = min(0.5 + 命中数*0.05, 0.9)`。

**开源分类**：对每个仓库在四类词表分别计数，取命中最多的一类；无命中归「其他」。

**简历匹配**：`coverage = |matched ∩ required| / |required|`，required 为目标 JD 技能 top15。

---

## 8. 降级与稳定性

| 场景 | 行为 |
| --- | --- |
| 代理未启动 | `http_get` 对 403/429/5xx/网络错误指数退避 3 次；错误按源记录，其余源继续 |
| `gh` 未登录 | GitHub 三路抓取返回错误字符串，缓存保留旧榜单，不影响其他模块 |
| arXiv/RSS 返回坏 XML | `parse_feed` 捕获 `ParseError` 返回空列表，记错误不崩 |
| 智联反爬 | 返回 0 岗位；`analyze_jd` 输出 `note`，大盘 talent 区块显示空状态 |
| feedparser 不存在 | 默认走标准库 `xml.etree` 解析；feedparser 仅是可选增强 |
| 缓存来自旧版本 | `migrate_cache` 幂等补键、清洗 HTML 残片、补 url/date/region |
| 日志很大 | `_tail` 从文件尾部按块读，内存有界 |

---

## 9. Makefile 自动化

| 类别 | target |
| --- | --- |
| 环境/校验 | `help` `install` `data-validate` `status` |
| 采集 | `fetch-news` `fetch-agent`（`fetch-github`/`fetch-arxiv` 为别名）`fetch-jd` `fetch-all` |
| 分析 | `analyze-news` `forecast` `rankings` `analyze-agent` `analyze-jd` `analyze-all` |
| 业务操作 | `resume` `apply-plan` `apply-packet` |
| 监控/大盘 | `monitor` `build-api` `serve` |
| 全局 | `pipeline` `cron` `test` `cache-init` `cache-clean` `clean` |

采集阶段**顺序执行**（不并行），便于逐模块看日志与限流；分析阶段全部离线、互不依赖，可用 `analyze-all` 一次跑完。`pipeline = data-validate → fetch-all → analyze-all → monitor → build-api`。

---

## 10. 性能实测

离线状态（样例缓存：资讯 600 条 / GitHub 150 仓库 / arXiv 125 篇，Python 3.15，单进程）：

| 阶段 | 实测耗时 | 说明 |
| --- | --- | --- |
| `modules.intel_network.cli --action analyze` | 0.18s | 含解释器启动约 0.13s |
| `modules.intel_network.cli --action forecast` | 0.20s | 规则匹配，无 LLM 调用 |
| `modules.intel_network.cli --action rankings` | 0.19s | 64 产品打分 + 150 仓库分类 |
| `modules.agent_lab.cli --action analyze` | 0.17s | — |
| `modules.monitor_center.cli` | 0.15s | 42 渠道表 + 反爬诊断 |
| `modules.dashboard.cli` | 0.13s | 契约 76KB JSON / 51KB JS |
| `make test`（10 项） | 0.03s | 纯离线 |

网络阶段未在实测中计入（当前环境未启代理）：资讯抓取受 420s 预算约束，GitHub 受 `gh` 限流影响。全部分析阶段都是 O(n) 扫描，数据量到上千条仍可接受。

---

## 11. 扩展指引

- **加资讯源**：改 `data/sources.json` 四元组，无需碰代码；`validate_data` 会检查总数，改数量时同步改校验。
- **加模型 / Agent**：改 `data/models.json` / `agents.json`；字段增删要同步 `frontend/app.js` 的表格列。
- **加开源分类桶**：在 `data/scoring.json:oss_categories` 加一组关键词；`oss_community_ranking` 自动生成新桶，前端遍历渲染无需改。
- **加大盘样式**：在 `data/themes.json` 加 theme + 在 `frontend/themes/` 加 CSS；`validate_data` 会检查文件存在。
- **加业务模块**：在 `modules/` 新建包，遵循 `collect 写缓存 / analyze 只读 / cli 入口` 三件套；在 `build_contract` 里加一个区块。
- **换 REST API**：用 Web 框架暴露 `api/contract.json`，前端把 `data.js` 改为 `fetch`。
- **投递适配器（慎做）**：`apply.py` 已把状态机与材料包解耦；若要自动提交，应新增显式授权的 adapter（凭据仅在运行时注入，不落盘），并默认 dry-run；对外提交前需人工确认。

---

## 12. 已知局限

1. `company_dynamics.json` 为人工整理的示例，`verified=false`，估值/营收等数字需二次核验；不代表实时事实。
2. 收费变动只做当前快照，暂无历史 diff（需快照表或定时任务留痕）。
3. 智联 JD 抓取受反爬影响大，常常 0 条；需要浏览器指纹/商业数据源才能稳定。
4. 简历优化是关键词对齐，不调用 LLM，也不理解语义；不可直接当求职材料提交。
5. `parse_feed` 用标准库 XML，遇到严重不合规的 HTML feed 会返回空；feedparser 可改善但非核心依赖。
6. 缓存单例按路径维护，但同一路径多次 `get_cache` 仍共享内存对象；测试需 `store._MANAGERS.clear()`。
7. 前端为自包含契约模式（不是 SPA 路由）；多页面/权限/VIP 等 0.1 路线图中的商业化功能未实现。

---

## 13. 代码审查要点

按以下不变量检查，见 `CODE_REVIEW.md` 的清单：

1. `modules/*/analyze.py` 不得写缓存、不得发网络请求（只允许读 `data/` + 缓存）。
2. `core/` 不得 import `modules/`；`data/*.json` 不得出现在 Python 字面量里。
3. 所有写盘走 `write_json` 原子写；所有外部文本进 DOM 前经过 `esc()`。
4. 新错误必须经过 `classify_error` 归类，能在监控中心看到改进建议。
5. 每次改动后跑 `make data-validate test build-api`，并用 `node --check frontend/app.js api/data.js` 验证前端。
