# GEO 引擎 · 本次交付概览

## 交付内容
一个**零依赖**（纯 Python 标准库）的 GEO（生成式引擎优化）引擎，覆盖企业实施 GEO 的六大操作：
结构化整理、自动化分发、语义增强、标准化格式输出、效果监测、可扩展性（多业务线接入）。

## 验证结果
- 端到端流水线 `run --all`：**两条业务线（弱电智能化 / 生态修复）全绿**
  - weakcurrent：5 文档 → 18 块 → 43 事实卡 / 18 问答对 / 7 术语 → 77 产物 → 16 次探测
  - greenenergy：2 文档 → 8 块 → 14 事实卡 / 8 问答对 / 5 术语 → 38 产物 → 10 次探测
- 冒烟测试 `python -m tests.test_smoke`：**10/10 通过**（模型/chunker/structure/semantic/formats/monitor/pipeline）
- CLI `check` / `list`：正常

## 本轮修复的质量问题
1. **小数点误断句**：`99.9%`、`15%~25%` 等数值不再被当句末切断（chunker + llm 分句正则修正）。
2. **引用句截断错位**：可引用片段补齐"主体名 + 时间 + 关键指标"，按 `MAX_CITABLE` 安全截断，不再把关键数字切掉。
3. **主题词猜测不准**：事实卡 topic 改从原文小标题路径派生，更稳。
4. **text 源读取 bug**：`TextReader` 原本只认 `options.content`，导致测试配置（用 `text`）读入 0 篇；已兼容 `content`/`text` 两种键。

## 关键设计决策
- **LLM 抽象为可插拔 Provider**：默认 `heuristic`（离线启发式），无 API Key 也能跑通；改配置即可切 `openai_compat` 接入真实模型。
- **组件注册表 + 配置驱动**：Reader/Chunker/LLM/Extractor/Publisher/Probe 全部插件化，新增能力或业务线只需加配置/注册组件。
- **SQLite 中介**：阶段间以库表为界，支持断点续跑与审计。

## 交付文件
- `geo_engine/`（引擎包，18 个模块）
- `business_lines/weakcurrent.json`、`greenenergy.json`（两条示例业务线）
- `config.json`、`content/`（示例内容）
- `tests/test_smoke.py`（冒烟测试）
- `README.md`（使用与架构文档）
- 运行产物：`dist/`（llms.txt / JSON-LD / 知识卡片 / 站点）、`reports/`（Markdown + HTML 看板）、`data/geo.db`

## 后续可扩展建议
- 接入真实模型与搜索 API 提升抽取/监测质量；
- 增加 `publisher: cdn` / `sitemap ping` 提升被收录速度；
- 监测接真实引擎后做"竞品 SOV"对照看板。
