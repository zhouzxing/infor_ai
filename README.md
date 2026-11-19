# AI 生态嗅探系统 · infor_ai_trands

按五大业务模块解耦重构的版本：**数据与实现分离、前后端解耦、Makefile 自动化、方便代码审查**。

## 五大模块

| 模块 | 目录 | 能力 |
| --- | --- | --- |
| ① 资讯网络 | `modules/intel_network/` | 42 源情报收集 / 热度趋势分析 / 新市场·产品形态·功能特性·敏感度预判 / 模型厂商·Agent厂商·开源社区（科研·工程·homelab·教育培训）四类榜单 |
| ② Agent 实验室 | `modules/agent_lab/` | Agent = Model + Harness；GitHub 热库 / arXiv 论文 / 商业动态（估值·营收·人事·产品线·收费变动）/ 研发动态 |
| ③ JD 雷达 | `modules/jd_radar/` | JD 抓取 / 热门·高薪·主力方向 / 简历针对性优化 / 投递与邀约本地台账（dry-run） |
| ④ 监控中心 | `modules/monitor_center/` | 数据源稳定性 / 反爬与工具诊断 / 日志管线审计 / 数据源商业价值评估 |
| ⑤ 大盘汇报 | `modules/dashboard/` + `frontend/` | 领导一眼看清的动态·机会·技术·人才大盘；政务 / 看板娘 / 预警三种样式 |

## 快速上手

```bash
make help             # 查看全部命令
make data-validate    # 校验数据层（42 源 / 权重 / 主题）
make pipeline         # 抓取 → 分析 → 监控 → 生成前端契约
make build-api        # 只重建 api/ 契约（离线）
make serve            # http://localhost:8080/frontend/index.html
make test             # 10 项离线单测，无需第三方依赖
```

离线演示（不想联网时）：`output/cache/ai_intel_cache.json` 已放一份 0.1 版本样例缓存，可直接跑
`make analyze-all monitor build-api` 查看大盘。该样例缓存属于 `output/`，已被 `.gitignore` 排除，
如需随代码分发请单独复制到 `data/fixtures/` 并调整 `.gitignore`。

### 前置条件

- Python 3.10+；核心路径**只用标准库**（RSS 解析已用 `xml.etree` 实现，不强制 feedparser）。
- 抓取需要代理：`export PROXY_HTTP=http://127.0.0.1:7897`（无代理时源会 403 / 超时）。
- GitHub 热榜需要 `gh auth login`；未登录时该源报错但不影响其他模块。

## 目录速览

```
infor_ai_trands/
├── data/          # 数据与实现分离：所有静态表都是 JSON（源/模型/Agent/评分/主题…）
├── core/          # 公共层：schema/store/http/feed/scoring/data_loader/cli
├── modules/       # 五大业务模块，每个模块 collect / analyze / cli
├── api/           # 前后端唯一契约：data.js + contract.json + manifest.json
├── frontend/      # 纯静态 HTML/CSS/JS，不含任何 Python 模板
├── output/        # 运行产物：cache / intel 分析 / jd 台账 / logs
├── tests/         # unittest 离线测试
├── scripts/       # status.py
├── docs/          # ARCHITECTURE.md / CODE_REVIEW.md
└── Makefile
```

## 常用命令

```bash
# 采集
make fetch-news          # 资讯网络（42 源，10 线程，420s 总预算）
make fetch-agent         # GitHub 热榜 + arXiv（fetch-github / fetch-arxiv 是别名）
make fetch-jd            # 智联 JD（反爬严格，可能 0 条）

# 分析（离线，只读缓存 + data/）
make analyze-news  forecast  rankings  analyze-agent  analyze-jd
make analyze-all         # 上面全部
make resume              # 需项目根目录放 resume.md
make apply-plan  apply-packet

# 监控 + 大盘
make monitor  build-api  serve
```

## 与 0.1 版本（infor_ai_lab）的区别

| 维度 | 0.1 | 本版本 |
| --- | --- | --- |
| 数据 | 60KB 的 `config/__init__.py` 内嵌数据字面量 | `data/*.json`，代码只加载；`make data-validate` 校验 |
| 分层 | 工具 / 缓存 / 智能 / 渲染四层，单文件巨型 `render` | `core` + 五大模块 + 契约 + 纯静态前端 |
| 前后端 | Python 直接生成整页 HTML | 后端只写 `api/data.js`，前端独立静态文件，可换 REST |
| 缓存 | 单例忽略后续路径参数、`get_cache` 跨调用污染 | 按路径维护 manager；原子写 + 幂等迁移 |
| 解析 | `re.split(r"[\s\p{P}]+")` 在 Python 会抛异常；正文里有未定义变量 | 标准库 XML 解析 + CJK 词元正则；全部有单测 |
| 监控 | 连败次数靠状态猜测 | 从 history 真实倒推；反爬错误分类到可操作建议 |

## 文档

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — 分层、数据流、算法、性能实测、扩展指引、已知局限
- [CODE_REVIEW.md](docs/CODE_REVIEW.md) — 代码审查地图与检查清单
