# GEO Engine (Generative Engine Optimization)

Help enterprises turn their professional content and data into **standardized formats that generative AI engines (ChatGPT, ERNIE, Qwen, Perplexity, etc.) can crawl, understand, and cite**, and continuously monitor how the business shows up in AI-generated answers.

Zero third-party dependencies — pure Python standard library. Works out of the box: the default **offline heuristic** path does extraction / enhancement / monitoring with no API key required; switching to a real LLM is a one-line config change (`provider: openai_compat`).

---

## Why GEO

Traditional SEO optimizes for blue links on search result pages. GEO (Generative Engine Optimization) optimizes for being **cited inside the generated answer** — the new surface where users actually get their answers. This engine structures your authoritative content so that when an AI engine is asked a question in your domain, it finds, trusts, and quotes you.

---

## Implemented capabilities

| # | Capability | Module | What it does |
|---|------------|--------|--------------|
| 1 | Structured organization | `structure.py` + `chunker.py` + `ingest.py` | Multi-source ingest → semantic chunking → extract entities / fact cards / Q&A pairs / glossary → build knowledge graph → quality scoring |
| 2 | Automated distribution | `distribute.py` + `pipeline.py` | Incremental publish to local static site / Git / HTTP / IndexNow; scheduler can periodically re-run |
| 3 | Semantic enhancement | `semantic.py` | Citable-snippet optimization, authority annotation, answer-first rewriting, entity alignment, intent-coverage analysis |
| 4 | Standardized output | `formats/` | `llms.txt` / `llms-full.txt` / JSON-LD (Organization · FAQ · Glossary · Facts · KnowledgeGraph) / knowledge cards / static site / sitemap |
| 5 | Effectiveness monitoring | `monitor.py` | Multi-engine probing (offline self-check / OpenAI-compat / search API), citation rate · mention rate · share-of-voice (SOV) · trend · sentiment |
| 6 | Extensibility | `registry.py` + `config.py` | Component registry + config-driven design; a business line = one JSON file; Reader / Chunker / LLM / Extractor / Publisher / Probe are all pluggable |

---

## Quick start

```bash
# 1) Environment self-check (prints registered components, dirs, business lines)
python -m geo_engine --root . check

# 2) List all business lines
python -m geo_engine --root . list

# 3) Run one business line end-to-end: ingest → structure → enhance → build → distribute → monitor → report
python -m geo_engine --root . run --bl weakcurrent

# 4) Run all business lines at once
python -m geo_engine --root . run --all

# 5) Run only some stages (resume / debug)
python -m geo_engine --root . run --bl weakcurrent --stages structure enhance build

# 6) Run smoke tests
python -m tests.test_smoke
```

> Requires Python 3.8+ (developed and tested on Python 3.13). No `pip install` needed.

---

## Directory layout

```
<root>/
  config.json                Global config (llm / monitor / log_level / layout; optional)
  business_lines/*.json      One config per business line (the multi-tenant entry point)
  content/<bl_id>/           Raw content for that line (.md / .txt / .csv / .jsonl / .html)
  dist/<bl_id>/              Generated artifacts (llms.txt, JSON-LD, knowledge cards, site)
  data/geo.db                SQLite store (docs / chunks / facts / qa / probe results; enables resume + audit)
  reports/<bl_id>/           Monitoring reports (Markdown + HTML dashboard)
```

---

## Add a business line (extensibility demo)

1. Create `<bl_id>.json` under `business_lines/`:

```json
{
  "id": "myline",
  "name": "Some Business Line",
  "description": "One-line positioning",
  "domain": "www.example.com",
  "authority": {
    "org_legal_name": "Example Technology Co., Ltd.",
    "website": "https://www.example.com",
    "certifications": ["ISO9001", "CCC"]
  },
  "sources": [
    {"type": "markdown_dir", "path": "content/myline"},
    {"type": "text", "options": {"content": "Structured text you can paste inline", "title": "Service Promise"}}
  ],
  "targets": [
    {"type": "local_static", "options": {"dir": "dist/myline"}}
  ],
  "monitor": {
    "engine": "heuristic",
    "queries": ["The 5~10 questions users most likely ask?"]
  }
}
```

2. Drop raw content into `content/myline/`, then run `run --bl myline`.

---

## Standardized outputs (why AI cites you more)

- **`llms.txt` + `llms-full.txt`**: Following the [llmstxt.org](https://llmstxt.org) spec — a machine-readable sitemap + citable-fact list for crawlers, far more controllable than letting an AI guess from raw HTML.
- **JSON-LD (schema.org)**: `Organization` / `FAQPage` / `DefinedTerm` / `Dataset` / `Graph` structured data, fed directly to engines that support structured extraction, with `author` / `dateModified` / `citation` to establish authority.
- **Knowledge cards (`cards/*.md`)**: One self-contained, citable snippet per fact / Q&A, carrying org name + date + key metrics — reducing the chance of truncation or misattribution.

---

## Connect a real LLM (optional)

Change the global or per-line `llm.provider` to `openai_compat` and set `base_url` / `api_key` / `model`:
- Structured extraction (`structure`) and semantic enhancement (`semantic`) switch to the LLM, with significantly higher quality.
- Monitoring `monitor.engine` can switch to `openai_compat` / `search_api` to hit real engines and search APIs.

The offline `heuristic` mode runs every stage; it only trails the LLM in extraction precision.

---

## Monitoring metrics

| Metric | Meaning |
|--------|---------|
| Mention rate | Share of answers that mention the brand / product |
| Citation rate | Share of answers that cite this site's domain |
| Avg. citation rank | Lower = more prominent (cited answers only) |
| Share of voice (SOV) | This site's citations ÷ (this site + competitors' citations) |
| Trend | Citation-rate change over the last 7 days vs. prior period |
| Intent coverage | coverage of informational / commercial / transactional intents, with gaps listed |

> The offline `heuristic` engine's "probe" answers the monitoring questions from your local knowledge-asset store — it tells you "when an AI crawls us, will it find material?". After wiring a real engine, the numbers reflect reality.

---

## Module index

```
geo_engine/
  models.py        Data models (BusinessLine / FactCard / QAPair / Term / KnowledgeGraph / ProbeResult / MetricsSnapshot)
  registry.py      Component registry (pluggability core)
  config.py        Config system (multi-line + path layout)
  store.py         SQLite store (resume + audit)
  llm.py           LLM Provider abstraction (heuristic / openai_compat)
  logutil.py       Logging
  ingest.py        Content ingest (reader plugins: markdown_dir / file / text / csv / jsonl / url / api)
  chunker.py       Semantic chunking
  structure.py     Structured organization (entity / fact / qa / term / graph / quality score)
  semantic.py      Semantic enhancement (citable / authority / answer-first / intent coverage)
  formats/         Standardized output (llms.txt / JSON-LD / cards / site / sitemap)
  distribute.py    Automated distribution (publisher plugins + incremental + scheduler)
  monitor.py       Monitoring (probe plugins + metrics + reports)
  pipeline.py      End-to-end orchestration (7 stages)
  cli.py           CLI entry
tests/test_smoke.py  Smoke tests (10 cases, covering the six modules + end-to-end)
```

---

## Documentation

- 🇨🇳 Chinese README: [README.zh-CN.md](README.zh-CN.md)
- 🇨🇳 Detailed Chinese usage guide: [docs/GEO使用说明.md](docs/GEO使用说明.md) — covers initialization, config params, submitting tasks, calling the generation interface, parsing outputs, limits, best practices, and troubleshooting.

---

## Extending

All core components are registered in `registry.py`. To add a new reader, publisher, extractor, or probe, subclass the base and register it:

```python
from geo_engine.registry import REGISTRY
from geo_engine.distribute import BasePublisher

@REGISTRY.register("publisher", "my_target")
class MyPublisher(BasePublisher):
    def publish(self, artifacts):
        ...  # your distribution logic
```

Then reference `"type": "my_target"` in a business line's `targets`.

---

## Contributing

Contributions are welcome. Please open an issue to discuss substantial changes first. Keep it dependency-free (standard library only) unless a new capability genuinely requires a third-party package.

## License

See [LICENSE](LICENSE) (to be added). The project is currently provided as-is for evaluation and integration.
