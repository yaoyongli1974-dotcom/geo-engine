# GEO 生成式引擎优化引擎 · 详细使用说明

> 版本：v1.0.0 ｜ 适用对象：开发者 / 技术运营 ｜ 运行环境：Python 3.9+（仅标准库，无第三方依赖）

本文档面向希望把企业专业内容接入生成式 AI 引擎（ChatGPT、Perplexity、Claude、Copilot、文心、通义等）并被其准确引用的团队。读完本文，您应能在 15 分钟内跑通一条业务线，并理解如何把它集成到自有系统中。

---

## 1. 核心功能与定位

**GEO（Generative Engine Optimization，生成式引擎优化）** 是 SEO 在"答案引擎"时代的延伸：它不追求搜索排名，而追求**让企业的专业内容成为大模型生成答案时的引用来源**。

本引擎的定位是一个**可编排的内容管线（Pipeline）平台**，把"散乱的企业资料"自动转成"AI 易抓取、易引用、带权威标注的标准化资产"，并持续分发、监测。它**不是**一个大模型，而是一个把企业内容与生成式引擎连接的**基础设施层**。

| 能力 | 说明 | 关键模块 |
|------|------|----------|
| 结构化整理 | 多源读取 → 语义分块 → 抽取实体/事实卡/问答对/术语/知识图谱 + 质量评分 | `ingest` `chunker` `structure` |
| 语义增强 | 可引用片段优化、权威 E-E-A-T 标注、答案优先改写、意图覆盖分析 | `semantic` |
| 标准化输出 | 生成 `llms.txt`/`llms-full.txt`、JSON-LD、知识卡片、静态站点、sitemap | `formats` |
| 自动化分发 | 增量发布到本地静态站 / Git / HTTP / IndexNow，支持调度 | `distribute` |
| 效果监测 | 多引擎探测 + 引用率/提及率/SOV/趋势/情感 + 报表看板 | `monitor` |
| 可扩展性 | 组件注册表 + 配置驱动，业务线即一份 JSON，全插件化 | `registry` |

**设计要点**

- **零依赖**：纯 Python 标准库实现，克隆即可运行，无需 `pip install`。
- **离线优先**：内置 `heuristic` 启发式 LLM（不联网、确定性），无 API Key 也能产出结构规范的可用版本；后续把 `provider` 改成 `openai_compat` 即可获得大模型增强。
- **阶段化 + SQLite 中介**：7 个阶段之间以 SQLite 落库为界，支持断点续跑、增量发布与审计追溯。
- **配置即业务线**：新增一条业务线 = 新增一份 JSON + 一个内容目录，无需改代码。

---

## 2. 适用应用场景

| 场景 | 说明 |
|------|------|
| 专业服务型企业对外知识门户 | 咨询、法律、医疗、工程类企业，把资质、标准、案例转成 AI 可引用资产 |
| 多业务线集团 | 每条业务线独立配置，统一引擎管理，互不干扰 |
| B2B 厂商产品/方案知识库 | 让采购者在 AI 问答中优先看到本品牌的产品参数、合规认证 |
| 本地化/垂直领域内容分发 | 通过 IndexNow、Git Pages 把内容推到生成式引擎可检索渠道 |
| 竞品声量（SOV）监测 | 追踪"行业问题"的 AI 答案中本品牌 vs 竞品的引用占比 |
| 内容团队自动化运营 | 定时增量分发 + 报表看板，替代手工维护 llms.txt |

**不适用 / 需改造的场景**：纯实时数据（需自建数据源 Reader）、需要真实联网多引擎探测（内置为启发式基线，见 §9 限制）、需要鉴权付费的搜索 API（需自定义 Probe）。

---

## 3. 总体架构与核心概念

```
企业资料(.md/.txt/.csv/API)
      │  ingest（多源读取）
      ▼
  SourceDoc（原始文档）
      │  structure（分块+抽取+评分）
      ▼
  Chunk / FactCard / QAPair / Term / KnowledgeGraph  ── 落 SQLite
      │  enhance（可引用化/权威标注/意图覆盖）
      ▼  落 SQLite
  SiteBuilder  ── build ──>  llms.txt / JSON-LD / 卡片 / 站点
      │  publish（增量分发）
      ▼
  本地站 / Git / HTTP / IndexNow
      │  monitor（探测）→ report（指标）
      ▼
  reports/ 上的 Markdown + HTML 看板
```

**核心概念**

- **业务线（BusinessLine）**：一切数据的作用域。系统是单实例、多业务线架构，每条业务线一份 `business_lines/<id>.json`。
- **阶段（Stage）**：`ingest → structure → enhance → build → publish → monitor → report`，可单跑可全跑。
- **组件注册表（Registry）**：Reader / LLM / Extractor / Publisher / Probe / Formatter 均以 `@REGISTRY.xxx("名字")` 注册，配置里写 `type` 即插即用。
- **store（SQLite）**：`data/geo.db`，阶段间数据交换与历史记录的唯一真相源。

---

## 4. 快速开始：从零到首份 llms.txt

### 4.1 环境准备

```bash
# 需要 Python 3.9+（已用 3.13 验证）
python --version

# 克隆并进入项目
cd GEO
python -m geo_engine.cli check      # 环境自检（列出已注册组件与目录状态）
```

`check` 正常会打印已注册组件（reader/publisher/formatter/llm 等）与目录检查结论。

### 4.2 初始化一条业务线

```bash
python -m geo_engine.cli init \
  --bl demo \
  --name "示例科技" \
  --domain "https://www.example.com" \
  --industry "弱电智能化" \
  --llm heuristic
```

该命令会：

1. 写入 `business_lines/demo.json`（缺省配置，`llm.provider=heuristic`）。
2. 创建 `content/demo/`（放企业资料）、`dist/demo/`（发布目录）、`reports/demo/`。

> 简写形式 `python -m geo_engine ...` 与 `python -m geo_engine.cli ...` 等价。

### 4.3 放入企业内容

把资料放进 `content/demo/`，支持格式：

- `*.md` / `*.txt`：直接解析（推荐用 Markdown 标题层级组织，便于分块与主题归属）
- `*.csv` / `*.jsonl`：结构化数据来源
- 也支持 `text`（内联）、`url`、`api` 等来源（见 §6.2）

示例 `content/demo/综合布线系统.md`：

```markdown
# 综合布线系统

## 六类线缆
我们采用六类非屏蔽双绞线，信道衰减 ≤ 19.8dB（100MHz），达到 ISO/IEC 11801 标准。
支持 10GBASE-T 传输距离 55 米，优于超五类方案。

## 资质
公司持有电子与智能化工程专业承包一级资质，并通过 ISO 9001 质量管理体系认证。
```

### 4.4 运行全链路

```bash
python -m geo_engine.cli run --bl demo
# 或跑全部业务线：
python -m geo_engine.cli run --all
```

成功后会看到各阶段产物统计。产物落在 `dist/demo/`：

```
dist/demo/
  llms.txt              ← 给大模型的索引入口
  llms-full.txt         ← 全文语料
  knowledge.html / faq.html / glossary.html / index.html
  cards/*.md            ← 知识卡片（每张事实/问答一个）
  data/*.jsonld         ← 结构化数据（Organization/FAQ/Glossary/Facts/Graph）
  sitemap.xml / robots.txt / feed.xml
```

把 `dist/demo/` 挂到 `https://www.example.com/geo/`（Nginx / 对象存储 / GitHub Pages 均可），在 `robots.txt` 与首页注明 `llms.txt` 位置，即可被生成式引擎抓取与引用。

---

## 5. 完整操作流程（端到端）

```
① 初始化    init ──────────────► business_lines/<id>.json + 目录
② 准备内容  往 content/<id>/ 放资料
③ 配置参数  编辑 business_lines/<id>.json（来源/目标/LLM/监测/权威信息）
④ 提交任务  run --bl <id>  [--stage …] [--force] [--no-llm]
      └─ 引擎"调用生成接口"：build 阶段渲染 llms.txt/JSON-LD/卡片
⑤ 获取结果  dist/<id>/ 文件 + reports/<id>/ 报表 + 返回 PipelineResult
⑥ 解析输出  读文件 / 读 SQLite / 用 Python API 取 Artifact 列表
⑦ 持续分发  publish（增量）+ schedule / crontab 定时触发
⑧ 效果监测  monitor ──► report（dashboard.html）
```

下文逐环节展开。

---

## 6. 配置参数详解

### 6.1 全局配置 `config.json`（可选）

位于项目根目录，控制路径布局与全局默认 LLM/监测：

```json
{
  "layout": { "business_lines": "business_lines", "content": "content",
              "dist": "dist", "data": "data", "reports": "reports" },
  "log_level": "INFO",
  "llm": { "provider": "heuristic" },
  "monitor": { "interval_hours": 24 }
}
```

> 不写 `config.json` 也能跑，全部走默认布局。若装了 PyYAML，配置文件也可写成 `.yaml`。

### 6.2 业务线配置 `business_lines/<id>.json`

顶层字段（`BusinessLine`）：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | str | — | 业务线唯一 ID（文件名即 ID，可省略） |
| `name` | str | "" | 展示名称 |
| `description` | str | "" | 简介（写入站点与 JSON-LD） |
| `domain` | str | "" | 主域名，用于 AI 引用归属判定 |
| `language` | str | "zh-CN" | 主语言 |
| `topics` | list[str] | [] | 核心主题词（影响术语/意图覆盖） |
| `audience` | list[str] | [] | 目标人群 |
| `competitors` | list[str] | [] | 竞品域名/品牌（SOV 计算用） |
| `authority` | obj | — | 权威信息（见下） |
| `sources` | list[obj] | [] | 内容来源（见下） |
| `targets` | list[obj] | [] | 分发目标（见下） |
| `llm` | obj | — | LLM provider 配置 |
| `monitor` | obj | — | 监测配置 |
| `options` | obj | {} | 扩展项（`options.formatter` 可指定自定义站点构建器） |

**权威信息 `authority`**（E-E-A-T 信号，强烈建议填全）：

| 字段 | 说明 |
|------|------|
| `org_legal_name` | 法律主体名称（引用归属主体） |
| `aliases` | 别名/简称，帮助 AI 做实体对齐 |
| `website` | 官网（引用 URL 基准） |
| `industry` / `region` / `founded` | 行业 / 区域 / 成立年份 |
| `certifications` / `standards` / `awards` | 资质 / 遵循标准 / 荣誉 |
| `authors` | 内容审校人 `[{name,title,credential}]` |
| `evidence_base` | 案例/报告/白皮书 `[{title,url}]` |

**内容来源 `sources[].type`**：

| type | path 含义 | 备注 |
|------|-----------|------|
| `markdown_dir` | 目录（相对根，默认 `content/<id>`） | 读取目录下所有 `.md/.txt` |
| `file` | 单个文件 | |
| `csv` | CSV 文件 | 列映射走 `options` |
| `jsonl` | JSONL 文件 | 每行一条记录 |
| `text` | 忽略 path | 正文放 `options.content`（也兼容 `options.text`） |
| `url` | 网页地址 | 需自定义 Reader 取内容 |
| `api` | 接口地址 | 需自定义 Reader |

每个来源可设 `authority`（1~5，越高越优先被引用）、`tags`。

**分发目标 `targets[].type`**（见 §7 各选项）：

`local_static` / `git` / `http` / `indexnow` / `noop`。

**LLM `llm`**：

| 字段 | 默认 | 说明 |
|------|------|------|
| `provider` | `heuristic` | `heuristic`（离线）或 `openai_compat` |
| `model` | `gpt-4o-mini` | |
| `base_url` | OpenAI 地址 | 可指向 DeepSeek/通义/月之暗面/vLLM 等兼容端点 |
| `api_key_env` | `OPENAI_API_KEY` | Key 从环境变量读取（**不入仓库**） |
| `temperature` | 0.2 | |
| `max_tokens` | 1200 | |
| `timeout` | 60 | 秒 |

**监测 `monitor`**：

| 字段 | 说明 |
|------|------|
| `engines` | 探测引擎列表（默认 `["generic"]`，内置为启发式基线） |
| `queries` | 要追踪的问题列表（建议覆盖核心意图） |
| `competitors` | 竞品域名/品牌 |
| `interval_hours` | 监测间隔 |

---

## 7. 自动化分发（持续更新与分发）

`publish` 阶段读取 `targets` 配置，按**增量**（内容指纹变化才发）逐个发布。内置发布器：

| type | 关键 options | 用途 |
|------|--------------|------|
| `local_static` | `dir`（必填，输出目录）；`clean`(bool) | 写入本地目录，挂 Nginx / 对象存储 |
| `git` | `repo`（必填本地仓库）、`remote`、`branch`、`push`、`subdir`、`commit_message` | 自动 commit/push（适配 GitHub Pages） |
| `http` | `url`（必填）、`headers`（支持 `${ENV}`）、`method`、`batch`、`timeout` | POST 到自建接口/CMS/Webhook |
| `indexnow` | `key`（支持 `${ENV}`）、`key_location`、`endpoint`、`host` | 主动提交 URL 给 Bing/Copilot 等 |
| `noop` | — | 演练，只统计不发 |

> 鉴权密钥一律用 `${ENV_NAME}` 写法从环境变量注入，避免明文入库（如 `headers: {"Authorization": "Bearer ${GEO_API_TOKEN}"}`）。

**定时触发**（推荐生产用系统 cron，而非内置阻塞调度器）：

```bash
# 生成一行 crontab（每天 03:00）
python -m geo_engine.cli crontab --bl demo --hour 3 --minute 0
# 输出示例：0 3 * * * /path/python -m geo_engine.cli --root /path/GEO run --bl demo >> /path/GEO/logs/demo.log 2>&1

# 或直接前台循环（测试/演示）：
python -m geo_engine.cli schedule --bl demo --hours 24
```

---

## 8. 提交任务与"调用生成接口"

引擎没有独立的网络"生成 API"，其"生成"就是 **Pipeline 的 `build` 阶段把资产渲染成 llms.txt / JSON-LD / 卡片**。调用方式有两种：

### 8.1 命令行（推荐日常使用）

```bash
# 全链路（含 build 生成）
python -m geo_engine.cli run --bl demo

# 只跑生成（build）及之前阶段
python -m geo_engine.cli run --bl demo --stage ingest --stage structure --stage enhance --stage build

# 跳过某阶段 / 强刷 / 强制离线
python -m geo_engine.cli build   --bl demo          # 仅 build
python -m geo_engine.cli run --bl demo --force       # 忽略增量，全量发布
python -m geo_engine.cli run --bl demo --no-llm      # 强制离线规则模式

# 单阶段子命令：ingest / structure / enhance / build / publish / monitor / report
python -m geo_engine.cli monitor --bl demo
python -m geo_engine.cli report  --bl demo
```

### 8.2 Python API（集成到自有系统）

```python
from geo_engine.config import load_settings
from geo_engine.store import Store
from geo_engine.pipeline import GeoPipeline

settings = load_settings(".")            # 项目根
pipe = GeoPipeline(settings, Store(settings.db_path))

res = pipe.run("demo", force=False, use_llm=True)
print(res.ok(), res.errors)             # 是否成功、错误列表
print([a.path for a in res.artifacts])  # 生成的产物清单（Artifact）

# 批量
for r in pipe.run_all():
    print(r.bl_id, r.ok())
```

`PipelineResult` 关键字段：`stages`（各阶段统计）、`artifacts`（产物列表）、`publish`（发布结果）、`metrics`（指标快照）、`errors`。

**切换到真实大模型增强**：把 `business_lines/demo.json` 里 `llm.provider` 改为 `openai_compat`，并设置环境变量 `OPENAI_API_KEY`（或指向兼容端点 `base_url`/`api_key_env`）。缺 Key 时引擎会**自动回退离线模式**并告警，不会中断。

---

## 9. 获取与解析输出结果

### 9.1 产物清单（落 `dist/<id>/`）

| 产物 | 格式 | 用途 |
|------|------|------|
| `llms.txt` | 文本 | 面向 LLM 的索引入口（列事实/问答/术语链接） |
| `llms-full.txt` | 文本 | 完整语料，供大模型整库读取 |
| `cards/*.md` | Markdown | 每张事实/问答一张卡片，AI 最易解析的载体 |
| `index.html` `knowledge.html` `faq.html` `glossary.html` | HTML | 人类可读知识中心（内嵌 JSON-LD） |
| `data/organization.jsonld` | JSON-LD | Organization 实体 |
| `data/faq.jsonld` | JSON-LD | FAQPage 结构化数据 |
| `data/glossary.jsonld` | JSON-LD | DefinedTermSet 术语表 |
| `data/facts.jsonld` | JSON-LD | 事实 ItemList |
| `data/knowledge-graph.json` | JSON | 知识图谱 |
| `data/index.json` | JSON | 资产索引（ID/URL/计数） |
| `sitemap.xml` `robots.txt` `feed.xml` | — | 收录与更新信号 |

### 9.2 如何解析输出

**方式 A — 直接读文件**：最常见。`llms.txt` 是纯文本，`*.jsonld` / `*.json` 是标准 JSON。

**方式 B — 读 SQLite 历史**：阶段间数据均落 `data/geo.db`，可查询事实卡、问答、探测记录、运行日志等，便于自建看板。

```python
from geo_engine.store import Store
s = Store("data/geo.db")
print(s.stats("demo"))                 # 资产规模
facts = [f for f in s.load_facts("demo")]
print(facts[0].citable)                # 一条可引用陈述
```

**方式 C — 用 PipelineResult**：见 §8.2，`res.artifacts` 是 `Artifact` 列表，每项含 `path/content/format/checksum`。

### 9.3 监测报表（落 `reports/<id>/`）

- `report-<时间戳>.md`：核心指标 + 趋势 + 分引擎 + 最佳问题 + 意图缺口。
- `dashboard.html`：可视化看板（浏览器打开）。

关键指标（`MetricsSnapshot`）：

| 指标 | 含义 |
|------|------|
| `mention_rate` | 提及率：问题中提到品牌/产品的比例 |
| `citation_rate` | 引用率：答案引用本站域名的比例 |
| `sov` | Share of Voice：本站引用数 /（本站 + 竞品引用数） |
| `avg_rank` | 平均引用位次（越小越优先） |
| `sentiment` | 情感倾向（-1~1） |
| `by_engine` | 分引擎表现 |
| `gaps` | 有曝光无引用的问题（优化机会） |

> ⚠️ **监测引擎说明**：`engine=local/generic` 时为**启发式基线**（基于事实/问答与"模拟答案"的匹配，用于本地验证管线与趋势基线）。要获得真实多引擎数据，需实现自定义 Probe（如调用 Perplexity/ChatGPT/搜索 API）并 `@REGISTRY.probe("xxx")` 注册，或在 `monitor.queries` 中接入真实抓取。详见 §12 扩展。

---

## 10. 常见使用限制

1. **内置监测为启发式基线**：不联网、不调用真实搜索引擎；真实 SOV 需自定义 Probe 或外部数据接入。
2. **离线（`heuristic`）质量有上限**：事实/问答抽取走规则，无法理解语义、易漏隐含信息；追求质量请接 `openai_compat` 或自建 provider。
3. **不替代人工审校**：自动生成的事实卡需业务方抽检，避免错误参数被 AI 引用传播。
4. **LLM 调用为同步阻塞**：`openai_compat` 为单次 HTTP 请求，大规模内容建议分批 + 并发改造或走异步。
5. **增量判定基于内容指纹**：移动/重命名文件会被当作"新增+删除"，可能触发全量发布；保持路径稳定。
6. **CSV/JSONL/URL/API 来源需自定义 Reader**：内置仅完整实现 `markdown_dir/file/text`；其余需注册 `reader`。
7. **密钥不入库**：`http`/`indexnow` 的鉴权必须用 `${ENV}`；`openai_compat` 的 Key 走环境变量。
8. **并行业务线**：`run_all` 为顺序执行；需要并发请自行多线程调用 `pipe.run(bl_id)`。

---

## 11. 最佳实践建议

1. **先离线跑通，再上模型**：用 `heuristic` 验证全链路与产物形态，确认无误后切 `openai_compat`，避免浪费 Token。
2. **填全 `authority`**：主体名称、资质、标准、官网是 AI 判断"是否该引用你"的核心信号，务必准确。
3. **内容用 Markdown 标题层级组织**：`#`→`##` 层级会直接影响分块与事实的主题归属，提升可引用性。
4. **事实自带"主体+数值+条件+时间"**：可被直接引用的陈述（如"XX 公司六类线缆信道衰减 ≤19.8dB@100MHz"）比模糊描述更易被引用。
5. **queries 要覆盖真实意图**：监测的 `queries` 应来自真实用户问法（informational/commercial/transactional 兼备），否则 SOV 失真。
6. **分发用 Git + Pages 或对象存储**：把 `dist/<id>/` 发布到 `https://主域/geo/`，并在 `robots.txt`、首页显著位置声明 `llms.txt`。
7. **提交 IndexNow**：内容更新后主动向 Bing/Copilot 提交 URL，加速被纳入语料。
8. **定时 + 增量**：用 `crontab` 每日跑全链路，`--force` 仅在重大改版时用；平时依赖增量省资源。
9. **gaps 反向优化内容**：报表里的 `gaps`（有曝光无引用）是最高优先级补内容清单。
10. **多业务线独立域名/目录**：每条业务线一个 `id`，共享引擎、互不污染，便于分权与审计。

---

## 12. 故障排查要点

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| `run` 报错"找不到业务线配置" | `business_lines/<id>.json` 不存在或文件名与 `--bl` 不符 | 用 `list` 查看已注册 ID；确认文件名即 ID |
| 某来源"读入 0 篇" | `text` 类型正文未放 `options.content`（或目录路径错） | 内联文本放 `options.content`（`text` 键也兼容）；`markdown_dir` 的 `path` 相对根目录 |
| LLM 阶段提示"回退离线模式" | `openai_compat` 缺 API Key / 网络不通 | 配置 `api_key_env` 对应环境变量；或暂用 `heuristic` |
| `openai_compat` 报 401/超时 | `base_url` 或 Key 错、需代理 | 检查端点与 Key；本机访问外网可能需要代理环境变量 |
| 发布 `failed: 缺少 options.dir/url/repo/key` | 目标 options 不全 | 对照 §6.2 / §7 补全必填项 |
| `git` 发布未推送 | `push=false` 或 `nothing to commit` | 确认 `options.push=true`；内容无变化则不提交 |
| 产物没更新 | 增量判定认为无变化 | 用 `--force` 强制全量；或确认内容确实改动 |
| 监测指标全 0 | 内置启发式基线 + queries 与事实无重叠 | 属预期；接真实 Probe 才有意义（见 §9.3） |
| `check` 提示目录缺失 | 尚未 `init` | 执行 `init` 创建骨架 |
| 想看详细错误栈 | 默认只报摘要 | 设环境变量 `GEO_DEBUG=1` 再运行 |

调试技巧：

```bash
python -m geo_engine.cli check               # 组件与目录自检
python -m geo_engine.cli list                # 业务线清单
python -m geo_engine.cli stats --bl demo     # 资产规模
GEO_DEBUG=1 python -m geo_engine.cli run --bl demo   # 打印异常栈
python -m tests.test_smoke                   # 冒烟测试（10 项）
```

---

## 13. 扩展开发（多业务线接入与自定义组件）

**新增业务线（无需写代码）**：复制 `business_lines/weakcurrent.json` → 改 `id` 与字段 → 在 `content/<新id>/` 放资料 → `run --bl <新id>`。

**自定义组件（插件）**：继承基类并用 Registry 装饰，配置里写 `type` 即可生效：

```python
from geo_engine.registry import REGISTRY
from geo_engine.distribute import BasePublisher
from geo_engine.models import BusinessLine, TargetConfig, PublishResult, Artifact

@REGISTRY.publisher("my_channel")
class MyPublisher(BasePublisher):
    def publish(self, artifacts: list[Artifact]) -> PublishResult:
        # 自行实现分发逻辑
        return self._ok(len(artifacts), message="已发布到我的渠道")
```

可扩展点：`reader`（来源）、`llm`（模型 provider，见 `register_provider`）、`extractor`（抽取器）、`publisher`（分发）、`probe`（监测探测）、`formatter`（站点构建器，经 `options.formatter` 指定）。

---

## 14. 命令行速查表

| 命令 | 作用 |
|------|------|
| `python -m geo_engine check` | 环境/组件自检 |
| `python -m geo_engine list` | 列出业务线 |
| `python -m geo_engine init --bl <id> --name <名> --domain <域> --llm heuristic` | 初始化业务线 |
| `python -m geo_engine run --bl <id>` | 跑全链路 |
| `python -m geo_engine run --all` | 跑全部业务线 |
| `python -m geo_engine run --bl <id> --stage build --force` | 指定阶段 + 强刷 |
| `python -m geo_engine <ingest\|structure\|enhance\|build\|publish\|monitor\|report> --bl <id>` | 单阶段 |
| `python -m geo_engine monitor --bl <id>` | 仅监测 |
| `python -m geo_engine report --bl <id>` | 仅报表 |
| `python -m geo_engine schedule --bl <id> --hours 24` | 阻塞式定时 |
| `python -m geo_engine crontab --bl <id> --hour 3` | 生成 crontab 行 |
| `python -m geo_engine stats --bl <id>` | 资产统计 |
| `python -m tests.test_smoke` | 冒烟测试 |

---

> 本文档与 `README.md`、`OVERVIEW.md` 配套使用。集成到自有系统时，推荐以 §8.2 的 Python API 为主入口，以 §6 的配置为接入面，以 §13 的 Registry 为扩展点。
