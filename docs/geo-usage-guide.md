# GEO Engine (Generative Engine Optimization) · Detailed Usage Guide

> Version: v1.0.0 ｜ Audience: Developers / Technical Ops ｜ Runtime: Python 3.9+ (standard library only, no third-party dependencies)

This document is for teams that want to feed their enterprise's professional content into generative AI engines (ChatGPT, Perplexity, Claude, Copilot, ERNIE, Qwen, etc.) and get it cited accurately. After reading this, you should be able to run a business line end-to-end within 15 minutes and understand how to integrate it into your own systems.

---

## 1. Core capabilities and positioning

**GEO (Generative Engine Optimization)** is the evolution of SEO for the "answer engine" era: instead of chasing search rankings, it pursues making **your enterprise's professional content a citation source when large models generate answers**.

This engine is positioned as an **orchestratable content pipeline platform** that automatically turns "scattered enterprise material" into "standardized assets that AIs can crawl, cite, and attribute with authority annotations", then continuously distributes and monitors them. It is **not** a large model itself; it is the **infrastructure layer** that connects enterprise content to generative engines.

| Capability | Description | Key modules |
|------------|-------------|-------------|
| Structured organization | Multi-source ingest → semantic chunking → extract entities / fact cards / Q&A pairs / glossary / knowledge graph + quality scoring | `ingest` `chunker` `structure` |
| Semantic enhancement | Citable-snippet optimization, authority (E-E-A-T) annotation, answer-first rewriting, intent-coverage analysis | `semantic` |
| Standardized output | Generate `llms.txt` / `llms-full.txt`, JSON-LD, knowledge cards, static site, sitemap | `formats` |
| Automated distribution | Incremental publish to local static site / Git / HTTP / IndexNow, with scheduling | `distribute` |
| Effectiveness monitoring | Multi-engine probing + citation rate / mention rate / SOV / trend / sentiment + report dashboard | `monitor` |
| Extensibility | Component registry + config-driven; a business line = one JSON file; fully pluggable | `registry` |

**Design highlights**

- **Zero dependencies**: Pure Python standard library. Clone and run — no `pip install`.
- **Offline-first**: Ships a `heuristic` heuristic LLM (no network, deterministic). Produces well-structured usable output with no API key; later switch `provider` to `openai_compat` for LLM enhancement.
- **Staged + SQLite mediator**: The 7 stages are bounded by SQLite persistence, supporting resume, incremental publishing, and audit traceability.
- **Config is the business line**: Adding a business line = adding one JSON + one content directory, no code change.

---

## 2. Applicable scenarios

| Scenario | Description |
|----------|-------------|
| Public knowledge portal for professional-services firms | Consulting, legal, medical, engineering firms turning credentials, standards, and cases into AI-citable assets |
| Multi-line conglomerate | Each business line independently configured, managed by one engine, isolated from each other |
| B2B vendor product / solution knowledge base | Let buyers see your brand's product specs and compliance certs first in AI answers |
| Localized / vertical content distribution | Push content to generative-engine-retrievable channels via IndexNow, Git Pages, etc. |
| Competitor share-of-voice (SOV) monitoring | Track the citation share of your brand vs. competitors across AI answers to "industry questions" |
| Content team automation | Scheduled incremental distribution + report dashboard, replacing manual `llms.txt` maintenance |

**Not applicable / needs customization**: pure real-time data (needs a custom data-source Reader), real networked multi-engine probing (built-in is a heuristic baseline — see §9 Limits), and paid authenticated search APIs (needs a custom Probe).

---

## 3. Overall architecture and core concepts

```
Enterprise material (.md/.txt/.csv/API)
      │  ingest (multi-source read)
      ▼
  SourceDoc (raw document)
      │  structure (chunk + extract + score)
      ▼
  Chunk / FactCard / QAPair / Term / KnowledgeGraph  ── persisted to SQLite
      │  enhance (citable / authority annotation / intent coverage)
      ▼  persisted to SQLite
  SiteBuilder  ── build ──>  llms.txt / JSON-LD / cards / site
      │  publish (incremental distribution)
      ▼
  Local site / Git / HTTP / IndexNow
      │  monitor (probe) → report (metrics)
      ▼
  Markdown + HTML dashboard on reports/
```

**Core concepts**

- **Business line (BusinessLine)**: The scope of all data. The system is single-instance, multi-tenant; each line is one `business_lines/<id>.json`.
- **Stage**: `ingest → structure → enhance → build → publish → monitor → report`; run individually or all at once.
- **Component registry (Registry)**: Reader / LLM / Extractor / Publisher / Probe / Formatter are all registered via `@REGISTRY.xxx("name")`; write `type` in config to plug in.
- **store (SQLite)**: `data/geo.db`, the single source of truth for inter-stage data exchange and history.

---

## 4. Quick start: from zero to your first llms.txt

### 4.1 Environment preparation

```bash
# Requires Python 3.9+ (validated on 3.13)
python --version

# Clone and enter the project
cd GEO
python -m geo_engine.cli check      # self-check (lists registered components and directory status)
```

`check` prints the registered components (reader / publisher / formatter / llm, etc.) and directory-check conclusions.

### 4.2 Initialize a business line

```bash
python -m geo_engine.cli init \
  --bl demo \
  --name "Demo Technology" \
  --domain "https://www.example.com" \
  --industry "Weak-current Intelligence" \
  --llm heuristic
```

This command will:

1. Write `business_lines/demo.json` (default config, `llm.provider=heuristic`).
2. Create `content/demo/` (drop enterprise material), `dist/demo/` (publish dir), `reports/demo/`.

> The short form `python -m geo_engine ...` is equivalent to `python -m geo_engine.cli ...`.

### 4.3 Add enterprise content

Drop material into `content/demo/`. Supported formats:

- `*.md` / `*.txt`: parsed directly (recommended to organize with Markdown heading hierarchy for better chunking and topic attribution)
- `*.csv` / `*.jsonl`: structured data sources
- Also supports `text` (inline), `url`, `api` sources (see §6.2)

Example `content/demo/structured-cabling.md`:

```markdown
# Structured Cabling System

## Category 6 cable
We use Category 6 unshielded twisted pair; channel attenuation ≤ 19.8dB (100MHz), meeting ISO/IEC 11801.
Supports 10GBASE-T transmission distance of 55 m, superior to Cat 5e solutions.

## Credentials
The company holds a Class-1 qualification for electronic and intelligent engineering contracting, and is certified to ISO 9001 quality management system.
```

### 4.4 Run the full pipeline

```bash
python -m geo_engine.cli run --bl demo
# or run all business lines:
python -m geo_engine.cli run --all
```

On success you'll see per-stage artifact stats. Artifacts land in `dist/demo/`:

```
dist/demo/
  llms.txt              ← index entry for LLMs
  llms-full.txt         ← full corpus
  knowledge.html / faq.html / glossary.html / index.html
  cards/*.md            ← knowledge cards (one per fact / Q&A)
  data/*.jsonld         ← structured data (Organization/FAQ/Glossary/Facts/Graph)
  sitemap.xml / robots.txt / feed.xml
```

Mount `dist/demo/` at `https://www.example.com/geo/` (Nginx / object storage / GitHub Pages all work), and declare the `llms.txt` location in `robots.txt` and on the homepage — then generative engines can crawl and cite it.

---

## 5. Complete workflow (end-to-end)

```
① Initialize   init ──────────────► business_lines/<id>.json + directories
② Prepare      put material into content/<id>/
③ Configure    edit business_lines/<id>.json (sources / targets / LLM / monitor / authority)
④ Submit task  run --bl <id>  [--stage …] [--force] [--no-llm]
      └─ engine "calls the generation interface": the build stage renders llms.txt / JSON-LD / cards
⑤ Get result   dist/<id>/ files + reports/<id>/ report + returned PipelineResult
⑥ Parse output read files / read SQLite / use Python API to get Artifact list
⑦ Keep distributing  publish (incremental) + schedule / crontab timed trigger
⑧ Monitor      monitor ──► report (dashboard.html)
```

Each step is detailed below.

---

## 6. Configuration parameter reference

### 6.1 Global config `config.json` (optional)

Located at the project root; controls the path layout and global default LLM / monitoring:

```json
{
  "layout": { "business_lines": "business_lines", "content": "content",
              "dist": "dist", "data": "data", "reports": "reports" },
  "log_level": "INFO",
  "llm": { "provider": "heuristic" },
  "monitor": { "interval_hours": 24 }
}
```

> It runs without `config.json`; everything falls back to the default layout. If PyYAML is installed, the config can also be written as `.yaml`.

### 6.2 Business-line config `business_lines/<id>.json`

Top-level fields (`BusinessLine`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | str | — | Unique business-line ID (the filename is the ID; can be omitted) |
| `name` | str | "" | Display name |
| `description` | str | "" | Short description (written to site and JSON-LD) |
| `domain` | str | "" | Primary domain, used for AI citation attribution |
| `language` | str | "zh-CN" | Primary language |
| `topics` | list[str] | [] | Core topic keywords (affect glossary / intent coverage) |
| `audience` | list[str] | [] | Target audiences |
| `competitors` | list[str] | [] | Competitor domains / brands (for SOV) |
| `authority` | obj | — | Authority info (see below) |
| `sources` | list[obj] | [] | Content sources (see below) |
| `targets` | list[obj] | [] | Distribution targets (see below) |
| `llm` | obj | — | LLM provider config |
| `monitor` | obj | — | Monitoring config |
| `options` | obj | {} | Extras (`options.formatter` can specify a custom site builder) |

**Authority info `authority`** (E-E-A-T signals, strongly recommended to fill fully):

| Field | Description |
|-------|-------------|
| `org_legal_name` | Legal entity name (citation attribution subject) |
| `aliases` | Aliases / short names, helps AI entity alignment |
| `website` | Official site (citation URL base) |
| `industry` / `region` / `founded` | Industry / region / founding year |
| `certifications` / `standards` / `awards` | Credentials / standards followed / honors |
| `authors` | Content reviewers `[{name,title,credential}]` |
| `evidence_base` | Cases / reports / whitepapers `[{title,url}]` |

**Content sources `sources[].type`**:

| type | path meaning | Notes |
|------|--------------|-------|
| `markdown_dir` | directory (relative to root, default `content/<id>`) | reads all `.md/.txt` in the dir |
| `file` | a single file | |
| `csv` | CSV file | column mapping via `options` |
| `jsonl` | JSONL file | one record per line |
| `text` | ignores path | body in `options.content` (also accepts `options.text`) |
| `url` | web URL | needs a custom Reader to fetch content |
| `api` | API endpoint | needs a custom Reader |

Each source can set `authority` (1~5, higher = cited first) and `tags`.

**Distribution targets `targets[].type`** (see §7 for options):

`local_static` / `git` / `http` / `indexnow` / `noop`.

**LLM `llm`**:

| Field | Default | Description |
|-------|---------|-------------|
| `provider` | `heuristic` | `heuristic` (offline) or `openai_compat` |
| `model` | `gpt-4o-mini` | |
| `base_url` | OpenAI endpoint | can point to DeepSeek / Qwen / Moonshot / vLLM compatible endpoints |
| `api_key_env` | `OPENAI_API_KEY` | Key read from env var (**never committed**) |
| `temperature` | 0.2 | |
| `max_tokens` | 1200 | |
| `timeout` | 60 | seconds |

**Monitoring `monitor`**:

| Field | Description |
|-------|-------------|
| `engines` | probe engine list (default `["generic"]`, built-in is a heuristic baseline) |
| `queries` | questions to track (recommended to cover core intents) |
| `competitors` | competitor domains / brands |
| `interval_hours` | monitoring interval |

---

## 7. Automated distribution (continuous update & distribution)

The `publish` stage reads the `targets` config and publishes incrementally (only when the content fingerprint changes). Built-in publishers:

| type | Key options | Purpose |
|------|-------------|---------|
| `local_static` | `dir` (required, output dir); `clean` (bool) | write to a local dir, mount on Nginx / object storage |
| `git` | `repo` (required local repo), `remote`, `branch`, `push`, `subdir`, `commit_message` | auto commit/push (fits GitHub Pages) |
| `http` | `url` (required), `headers` (supports `${ENV}`), `method`, `batch`, `timeout` | POST to a self-built interface / CMS / Webhook |
| `indexnow` | `key` (supports `${ENV}`), `key_location`, `endpoint`, `host` | proactively submit URLs to Bing / Copilot etc. |
| `noop` | — | dry run, only counts, does not send |

> Auth secrets must always be injected via the `${ENV_NAME}` syntax from environment variables, never committed in plaintext (e.g. `headers: {"Authorization": "Bearer ${GEO_API_TOKEN}"}`).

**Scheduled trigger** (recommended: system cron in production, not the built-in blocking scheduler):

```bash
# Generate a crontab line (daily at 03:00)
python -m geo_engine.cli crontab --bl demo --hour 3 --minute 0
# Example output: 0 3 * * * /path/python -m geo_engine.cli --root /path/GEO run --bl demo >> /path/GEO/logs/demo.log 2>&1

# Or a foreground loop (testing / demo):
python -m geo_engine.cli schedule --bl demo --hours 24
```

---

## 8. Submitting tasks and "calling the generation interface"

The engine has no separate networked "generation API" — its "generation" is the Pipeline's **`build` stage rendering assets into llms.txt / JSON-LD / cards**. Two ways to invoke it:

### 8.1 Command line (recommended for daily use)

```bash
# Full pipeline (includes build generation)
python -m geo_engine.cli run --bl demo

# Run only generation (build) and prior stages
python -m geo_engine.cli run --bl demo --stage ingest --stage structure --stage enhance --stage build

# Skip a stage / force / force offline
python -m geo_engine.cli build   --bl demo          # build only
python -m geo_engine.cli run --bl demo --force       # ignore incremental, full publish
python -m geo_engine.cli run --bl demo --no-llm      # force offline rule mode

# Single-stage subcommands: ingest / structure / enhance / build / publish / monitor / report
python -m geo_engine.cli monitor --bl demo
python -m geo_engine.cli report  --bl demo
```

### 8.2 Python API (integrate into your own system)

```python
from geo_engine.config import load_settings
from geo_engine.store import Store
from geo_engine.pipeline import GeoPipeline

settings = load_settings(".")            # project root
pipe = GeoPipeline(settings, Store(settings.db_path))

res = pipe.run("demo", force=False, use_llm=True)
print(res.ok(), res.errors)             # success?, error list
print([a.path for a in res.artifacts])  # generated artifact list (Artifact)

# Batch
for r in pipe.run_all():
    print(r.bl_id, r.ok())
```

`PipelineResult` key fields: `stages` (per-stage stats), `artifacts` (artifact list), `publish` (publish result), `metrics` (metrics snapshot), `errors`.

**Switch to real LLM enhancement**: change `llm.provider` in `business_lines/demo.json` to `openai_compat`, and set the env var `OPENAI_API_KEY` (or point to a compatible endpoint via `base_url` / `api_key_env`). When the key is missing, the engine **auto-falls back to offline mode** with a warning and does not break.

---

## 9. Getting and parsing output results

### 9.1 Artifact list (in `dist/<id>/`)

| Artifact | Format | Purpose |
|----------|--------|---------|
| `llms.txt` | text | LLM-facing index entry (lists fact / Q&A / term links) |
| `llms-full.txt` | text | full corpus for whole-library LLM reading |
| `cards/*.md` | Markdown | one card per fact / Q&A, the most AI-parseable carrier |
| `index.html` `knowledge.html` `faq.html` `glossary.html` | HTML | human-readable knowledge hub (embeds JSON-LD) |
| `data/organization.jsonld` | JSON-LD | Organization entity |
| `data/faq.jsonld` | JSON-LD | FAQPage structured data |
| `data/glossary.jsonld` | JSON-LD | DefinedTermSet glossary |
| `data/facts.jsonld` | JSON-LD | fact ItemList |
| `data/knowledge-graph.json` | JSON | knowledge graph |
| `data/index.json` | JSON | asset index (ID / URL / counts) |
| `sitemap.xml` `robots.txt` `feed.xml` | — | indexing and update signals |

### 9.2 How to parse the output

**Method A — read files directly**: most common. `llms.txt` is plain text; `*.jsonld` / `*.json` are standard JSON.

**Method B — read SQLite history**: inter-stage data persists to `data/geo.db`; query fact cards, Q&A, probe records, run logs, etc., for a custom dashboard.

```python
from geo_engine.store import Store
s = Store("data/geo.db")
print(s.stats("demo"))                 # asset scale
facts = [f for f in s.load_facts("demo")]
print(facts[0].citable)                # a citable statement
```

**Method C — use PipelineResult**: see §8.2; `res.artifacts` is a list of `Artifact`, each with `path / content / format / checksum`.

### 9.3 Monitoring report (in `reports/<id>/`)

- `report-<timestamp>.md`: core metrics + trend + per-engine + best questions + intent gaps.
- `dashboard.html`: visual dashboard (open in browser).

Key metrics (`MetricsSnapshot`):

| Metric | Meaning |
|--------|---------|
| `mention_rate` | Mention rate: share of questions where the brand / product is mentioned |
| `citation_rate` | Citation rate: share of answers citing this site's domain |
| `sov` | Share of Voice: this site's citations ÷ (this site + competitor citations) |
| `avg_rank` | Average citation rank (lower = more prominent) |
| `sentiment` | Sentiment (-1~1) |
| `by_engine` | Per-engine performance |
| `gaps` | Questions with exposure but no citation (optimization opportunities) |

> ⚠️ **Monitoring engine note**: when `engine=local/generic`, this is a **heuristic baseline** (matching facts / Q&A against a "simulated answer", for local pipeline validation and trend baselines). For real multi-engine data, implement a custom Probe (e.g. calling Perplexity / ChatGPT / search API) and register it via `@REGISTRY.probe("xxx")`, or wire real fetching into `monitor.queries`. See §12 Extensibility.

---

## 10. Common usage limits

1. **Built-in monitoring is a heuristic baseline**: no network, no real search engine calls; real SOV needs a custom Probe or external data.
2. **Offline (`heuristic`) quality has a ceiling**: fact / Q&A extraction is rule-based and cannot understand semantics or catch implicit info; for quality, use `openai_compat` or a custom provider.
3. **Does not replace human review**: auto-generated fact cards should be spot-checked by the business side to avoid wrong parameters being cited and propagated.
4. **LLM calls are synchronous blocking**: `openai_compat` is a single HTTP request; for large content, batch + concurrency or async is recommended.
5. **Incremental judgment is fingerprint-based**: moving / renaming a file is treated as "add + delete" and may trigger a full publish; keep paths stable.
6. **CSV/JSONL/URL/API sources need a custom Reader**: built-in fully implements only `markdown_dir / file / text`; others require a registered `reader`.
7. **Secrets are never committed**: auth for `http` / `indexnow` must use `${ENV}`; `openai_compat` key comes from env var.
8. **Concurrent business lines**: `run_all` runs sequentially; for concurrency, call `pipe.run(bl_id)` from multiple threads yourself.

---

## 11. Best-practice recommendations

1. **Run offline first, then add the model**: use `heuristic` to validate the full pipeline and artifact shape; switch to `openai_compat` only after confirming correctness, to avoid wasting tokens.
2. **Fill in `authority` completely**: entity name, credentials, standards, and official site are the core signals for AI deciding "whether to cite you" — be accurate.
3. **Organize content with Markdown heading hierarchy**: `#`→`##` levels directly affect chunking and a fact's topic attribution, improving citability.
4. **Facts carry "subject + value + condition + time"**: directly citable statements (e.g. "XX Corp Cat-6 cable channel attenuation ≤19.8dB@100MHz") are cited far more than vague descriptions.
5. **`queries` must cover real intents**: monitoring `queries` should come from real user phrasing (informational / commercial / transactional together); otherwise SOV is distorted.
6. **Distribute via Git + Pages or object storage**: publish `dist/<id>/` to `https://primary-domain/geo/`, and declare `llms.txt` in `robots.txt` and a prominent spot on the homepage.
7. **Submit IndexNow**: after content updates, proactively submit URLs to Bing / Copilot to speed inclusion into the corpus.
8. **Schedule + incremental**: use `crontab` to run the full pipeline daily; use `--force` only on major revisions; otherwise rely on incremental to save resources.
9. **Use `gaps` to reverse-optimize content**: `gaps` in the report (exposure without citation) is the highest-priority content-fill list.
10. **Independent domain / directory per business line**: one `id` per line, shared engine, no cross-contamination, easy to split ownership and audit.

---

## 12. Troubleshooting

| Symptom | Possible cause | Fix |
|---------|---------------|-----|
| `run` errors "business line config not found" | `business_lines/<id>.json` missing or filename ≠ `--bl` | use `list` to see registered IDs; filename is the ID |
| A source "read 0 docs" | `text` body not in `options.content` (or wrong dir path) | inline text goes in `options.content` (`text` key also accepted); `markdown_dir` `path` is relative to root |
| LLM stage says "falling back to offline" | `openai_compat` missing API key / no network | set the env var for `api_key_env`; or use `heuristic` for now |
| `openai_compat` returns 401 / timeout | wrong `base_url` or key, needs proxy | check endpoint and key; local outbound may need proxy env vars |
| Publish `failed: missing options.dir/url/repo/key` | target options incomplete | complete required fields per §6.2 / §7 |
| `git` publish did not push | `push=false` or `nothing to commit` | confirm `options.push=true`; no change = no commit |
| Artifacts not updated | incremental judged no change | use `--force` for full rebuild; or confirm content actually changed |
| Monitoring metrics all 0 | built-in heuristic baseline + queries don't overlap facts | expected; real Probe needed for meaning (see §9.3) |
| `check` says directory missing | not yet `init` | run `init` to create the skeleton |
| Want the full error stack | only summary reported by default | set env var `GEO_DEBUG=1` then run |

Debugging tips:

```bash
python -m geo_engine.cli check               # component & directory self-check
python -m geo_engine.cli list                # business-line list
python -m geo_engine.cli stats --bl demo     # asset scale
GEO_DEBUG=1 python -m geo_engine.cli run --bl demo   # print exception stack
python -m tests.test_smoke                   # smoke tests (10 cases)
```

---

## 13. Extensibility (multi-line onboarding and custom components)

**Add a business line (no code)**: copy `business_lines/weakcurrent.json` → change `id` and fields → put material in `content/<new-id>/` → `run --bl <new-id>`.

**Custom component (plugin)**: subclass the base and decorate with the Registry; write `type` in config to activate:

```python
from geo_engine.registry import REGISTRY
from geo_engine.distribute import BasePublisher
from geo_engine.models import BusinessLine, TargetConfig, PublishResult, Artifact

@REGISTRY.publisher("my_channel")
class MyPublisher(BasePublisher):
    def publish(self, artifacts: list[Artifact]) -> PublishResult:
        # implement your distribution logic
        return self._ok(len(artifacts), message="Published to my channel")
```

Extensible points: `reader` (sources), `llm` (model provider, see `register_provider`), `extractor` (extractors), `publisher` (distribution), `probe` (monitoring probes), `formatter` (site builder, via `options.formatter`).

---

## 14. Command-line quick reference

| Command | Purpose |
|---------|---------|
| `python -m geo_engine check` | environment / component self-check |
| `python -m geo_engine list` | list business lines |
| `python -m geo_engine init --bl <id> --name <name> --domain <domain> --llm heuristic` | initialize a business line |
| `python -m geo_engine run --bl <id>` | run full pipeline |
| `python -m geo_engine run --all` | run all business lines |
| `python -m geo_engine run --bl <id> --stage build --force` | specific stage + force rebuild |
| `python -m geo_engine <ingest\|structure\|enhance\|build\|publish\|monitor\|report> --bl <id>` | single stage |
| `python -m geo_engine monitor --bl <id>` | monitoring only |
| `python -m geo_engine report --bl <id>` | report only |
| `python -m geo_engine schedule --bl <id> --hours 24` | blocking scheduler |
| `python -m geo_engine crontab --bl <id> --hour 3` | generate a crontab line |
| `python -m geo_engine stats --bl <id>` | asset stats |
| `python -m tests.test_smoke` | smoke tests |

---

> This document is used together with `README.md` and `OVERVIEW.md`. When integrating into your own system, the recommended entry point is the Python API in §8.2, the configuration surface in §6, and the Registry in §13 as the extension point.
