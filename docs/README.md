# infor_ai_lab 文档

| 文档 | 内容 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架构设计 + 实现：数据流、抓取/渲染两层职责、五 tab 结构、部署 cron、故障排查、维护与升级路径 |
| [DATA_SCHEMA.md](DATA_SCHEMA.md) | `ai_intel_cache.json` 字段级契约 + 两张静态大表结构 + schema 演进规则 |

快速上手：
```bash
python3 ai_intel_aggregator.py   # 抓数据 + 生成 index.html（需代理 127.0.0.1:7897；GitHub 榜需 gh 登录）
open index.html                   # 自包含页面，双击即开
```

相关历史材料：`../CODE_REVIEW.md`（与源码同级，按 v6 行号索引；行号已漂移，仅作结构参考）。
