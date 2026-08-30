# GEO 引擎（生成式引擎优化）

帮助企业把专业内容与数据，整理成**生成式 AI 引擎（ChatGPT、文心、通义、Perplexity 等）易于抓取、理解并引用**的标准化格式，并持续监测企业在 AI 生成回答中的展现情况。

零第三方依赖，纯 Python 标准库实现，开箱即用：默认走**离线启发式**抽取/增强/监测，无需任何 API Key 即可跑通全链路；接入真实大模型只需改一处配置（`provider: openai_compat`）。

---

## 已实现的六大能力

| 能力 | 模块 | 说明 |
|------|------|------|
| 1. 结构化整理 | `structure.py` + `chunker.py` + `ingest.py` | 多源读取 → 语义分块 → 抽取实体/事实卡/问答对/术语表 → 构建知识图谱 → 质量评分 |
| 2. 自动化分发 | `distribute.py` + `pipeline.py` | 增量发布到本地静态站/Git/HTTP/IndexNow；调度器可周期巡检 |
| 3. 内容语义增强 | `semantic.py` | 可引用片段优化、权威信息标注、答案优先改写、实体对齐、意图覆盖分析 |
| 4. 标准化格式输出 | `formats/` | `llms.txt` / `llms-full.txt` / JSON-LD（Organization·FAQ·Glossary·Facts·KnowledgeGraph）/ 知识卡片 / 静态站点 / sitemap |
| 5. 效果监测 | `monitor.py` | 多引擎探测（离线自检/OpenAI 兼容/搜索 API）、引用率·提及率·声量份额 SOV·趋势·情感 |
| 6. 可扩展性 | `registry.py` + `config.py` | 组件注册表 + 配置驱动；业务线 = 一份 JSON；Reader/Chunker/LLM/Extractor/Publisher/Probe 全部插件化 |

---

## 快速开始

```bash
# 1) 环境自检（打印已注册组件、目录、业务线）
python -m geo_engine --root . check

# 2) 列出所有业务线
python -m geo_engine --root . list

# 3) 跑通某条业务线全链路：接入→整理→增强→构建→分发→监测→报表
python -m geo_engine --root . run --bl weakcurrent

# 4) 一次跑全部业务线
python -m geo_engine --root . run --all

# 5) 只跑某些阶段（断点续跑 / 调试）
python -m geo_engine --root . run --bl weakcurrent --stages structure enhance build

# 6) 运行冒烟测试
python -m tests.test_smoke
```

> 运行环境：项目自带 Python 3.13（`.workbuddy/binaries/python/versions/3.13.12/python.exe`）。

---

## 目录约定

```
<root>/
  config.json                全局配置（llm/monitor/log_level/layout，可选）
  business_lines/*.json      每条业务线一份配置（多业务线接入点）
  content/<bl_id>/           该业务线的原始内容（.md / .txt / .csv / .jsonl / .html）
  dist/<bl_id>/              生成的发布产物（llms.txt、JSON-LD、知识卡片、站点）
  data/geo.db                SQLite 存储（文档/块/事实/问答/探测结果，支撑续跑与审计）
  reports/<bl_id>/           效果监测报表（Markdown + HTML 看板）
```

---

## 新增一条业务线（可扩展性示范）

1. 在 `business_lines/` 下新建 `<bl_id>.json`：

```json
{
  "id": "myline",
  "name": "某业务线",
  "description": "一句话定位",
  "domain": "www.example.com",
  "authority": {
    "org_legal_name": "示例科技有限公司",
    "website": "https://www.example.com",
    "certifications": ["ISO9001", "CCC"]
  },
  "sources": [
    {"type": "markdown_dir", "path": "content/myline"},
    {"type": "text", "options": {"content": "可直接贴的结构化文本", "title": "服务承诺"}}
  ],
  "targets": [
    {"type": "local_static", "options": {"dir": "dist/myline"}}
  ],
  "monitor": {
    "engine": "heuristic",
    "queries": ["用户最可能问的 5~10 个问题？"]
  }
}
```

2. 把原始内容放进 `content/myline/`，执行 `run --bl myline` 即可。

---

## 标准化输出说明（为什么 AI 更爱引用）

- **`llms.txt` + `llms-full.txt`**：仿 Ansible/llmstxt.org 规范，给爬虫一份"机器可读的站点地图 + 可引用事实清单"，比让 AI 自己从 HTML 里猜更可控。
- **JSON-LD（schema.org）**：`Organization`/`FAQPage`/`DefinedTerm`/`Dataset`/`Graph` 五类结构化数据，直接喂给支持结构化抽取的引擎，并标注 `author`/`dateModified`/`citation` 以建立权威性。
- **知识卡片（`cards/*.md`）**：每条事实/问答一张自足、带主体名+时间+关键指标的可引用片段，降低被截断或误归属的概率。

---

## 接入真实大模型（可选）

把全局或业务线 `llm.provider` 改为 `openai_compat`，并配置 `base_url` / `api_key` / `model`：
- 结构化抽取（`structure`）与语义增强（`semantic`）会改用大模型，质量显著提升；
- 监测 `monitor.engine` 可改为 `openai_compat` / `search_api`，对接真实引擎与搜索 API。

离线 `heuristic` 模式下所有环节均可运行，仅抽取精度弱于大模型。

---

## 效果监测指标口径

| 指标 | 含义 |
|------|------|
| 提及率 mention_rate | 回答中出现品牌/产品的比例 |
| 引用率 citation_rate | 回答引用本站域名的比例 |
| 平均引用位次 rank | 越小越靠前（仅统计已引用） |
| 声量份额 SOV | 本站引用数 /（本站 + 竞品引用数） |
| 趋势 trend | 近 7 天相对上一周期的引用率变化 |
| 意图覆盖 coverage | informational/commercial/transactional 等意图的覆盖度，列出缺口 |

> 离线 `heuristic` 引擎的"探测"是用本地知识资产库去回答监测问题，用于回答"AI 来抓我们时找得到素材吗"；接入真实引擎后数据即贴近现实。

---

## 模块索引

```
geo_engine/
  models.py        数据模型（BusinessLine / FactCard / QAPair / Term / KnowledgeGraph / ProbeResult / MetricsSnapshot）
  registry.py      组件注册中心（插件化核心）
  config.py        配置系统（多业务线 + 路径布局）
  store.py         SQLite 存储层（断点续跑 + 审计）
  llm.py           LLM Provider 抽象（heuristic / openai_compat）
  logutil.py       日志
  ingest.py        内容接入（reader 插件：markdown_dir/file/text/csv/jsonl/url/api）
  chunker.py       语义分块
  structure.py     结构化整理（实体/事实/问答/术语/图谱/质量评分）
  semantic.py      语义增强（可引用化/权威标注/答案优先/意图覆盖）
  formats/         标准化输出（llms.txt / JSON-LD / 卡片 / 站点 / sitemap）
  distribute.py    自动化分发（publisher 插件 + 增量 + 调度）
  monitor.py       效果监测（probe 插件 + 指标计算 + 报表）
  pipeline.py      端到端编排（7 阶段）
  cli.py           命令行入口
tests/test_smoke.py 冒烟测试（10 项，覆盖六大模块 + 端到端）
```

---

## 多用户 / 服务器端部署

引擎还提供**多租户服务端**（`geo_web` 包），且**不改动核心引擎 `geo_engine` 算法**：

- **租户隔离**：每租户独立 SQLite 库（`tenants/<tid>/geo.db`）+ WAL；产物按 `tenants/<tid>/dist/<bl>/` 命名空间隔离。
- **鉴权**：JWT（HS256，标准库实现）+ PBKDF2 口令哈希；可插拔 **OAuth / 企业微信** 登录。
- **后台作业**：长耗时 `GeoPipeline.run()` 异步化（线程池），以 `job_id` 轮询。
- **REST 接口**：注册/登录/OAuth、业务线、内容上传、运行、产物、报表、健康检查。

```bash
pip install -r requirements.txt
export GEO_DATA_DIR=/var/lib/geo/data GEO_JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
python -m geo_web.server        # http://0.0.0.0:8000
```

→ 完整接口文档：[docs/geo-web-api.md](docs/geo-web-api.md) · 架构与实施计划：[docs/architecture-multiuser.md](docs/architecture-multiuser.md)
