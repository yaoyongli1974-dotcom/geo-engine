"""效果监测 —— 追踪企业在生成式搜索结果中的展现情况。

三类探测器（Probe）：
    heuristic     离线自检：用已发布的知识资产库回答监测问题，评估"答案覆盖率"与"素材完备度"。
                  不联网、零成本，用于在没有引擎 API 权限时建立基线与发现内容缺口。
    openai_compat 通用生成式引擎探测：调用兼容 OpenAI 协议的接口（含 Perplexity 等带 citations
                  的实现），判断回答是否提及品牌、是否引用本站域名、竞争对手是否出现。
    search_api    搜索/问答 API 探测：GET 一个可配置接口，从返回结果里找本站 URL 及其位次。

指标：提及率、引用率、平均引用位次、SOV（相对竞品的声量份额）、情感倾向、缺口清单。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .logutil import get_logger
from .models import (
    BusinessLine,
    FactCard,
    MetricsSnapshot,
    ProbeResult,
    QAPair,
    utcnow,
)
from .registry import REGISTRY

log = get_logger("monitor")

POSITIVE = ("领先", "专业", "可靠", "认证", "优秀", "推荐", "成熟", "稳定", "权威", "优质", "完善")
NEGATIVE = ("不足", "缺陷", "较差", "投诉", "问题", "落后", "风险", "失败", "不推荐", "遗憾")
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


# ---------------------------------------------------------------- 探测器基类

class BaseProbe(ABC):
    """探测器基类。"""

    def __init__(self, bl: BusinessLine, engine: str, options: Optional[Dict[str, Any]] = None) -> None:
        self.bl = bl
        self.engine = engine
        self.options = options or {}
        self.domain = _domain_of(bl.authority.website or bl.domain or "")
        self.brand = bl.authority.org_legal_name or bl.name or bl.id
        self.aliases = [a for a in bl.authority.aliases if a]
        self.competitors = list(bl.monitor.competitors or bl.competitors)

    @abstractmethod
    def fetch(self, query: str) -> Dict[str, Any]:
        """返回 {'answer': str, 'citations': [url...]}。"""

    def probe(self, query: str) -> ProbeResult:
        try:
            raw = self.fetch(query)
        except Exception as exc:
            log.warning("[%s/%s] 探测失败 %s: %s", self.bl.id, self.engine, query, exc)
            return ProbeResult(business_line=self.bl.id, engine=self.engine, query=query,
                               raw={"error": str(exc)})
        answer = str(raw.get("answer") or "")
        citations = [str(c) for c in (raw.get("citations") or [])]
        urls = citations + _URL_RE.findall(answer)
        cited_domains = sorted({_domain_of(u) for u in urls if _domain_of(u)})

        mentioned = any(n in answer for n in [self.brand, *self.aliases])
        if self.domain and self.domain in answer:
            mentioned = True
        cited = bool(self.domain) and any(self.domain in d for d in cited_domains)
        comps = sorted({c for c in self.competitors
                        if c and (c in answer or any(c in d for d in cited_domains))})
        rank = 0
        if cited:
            for i, d in enumerate(cited_domains, 1):
                if self.domain in d:
                    rank = i
                    break
        return ProbeResult(
            business_line=self.bl.id,
            engine=self.engine,
            query=query,
            mentioned=mentioned,
            cited=cited,
            cited_domains=cited_domains,
            competitors_mentioned=comps,
            rank=rank,
            sentiment=_sentiment(answer),
            answer_snippet=answer[:600],
            raw={"urls": urls[:20]},
        )


# ---------------------------------------------------------------- 离线自检探测

@REGISTRY.probe("heuristic")
class HeuristicProbe(BaseProbe):
    """离线自检：用本地知识资产库去"回答"监测问题。

    这不是真实引擎结果，但能回答一个关键问题：
    「如果 AI 引擎来抓我们的内容，它能找到回答这个问题的素材吗？」
    """

    def __init__(self, bl: BusinessLine, engine: str = "local",
                 options: Optional[Dict[str, Any]] = None,
                 facts: Optional[List[FactCard]] = None,
                 qas: Optional[List[QAPair]] = None) -> None:
        super().__init__(bl, engine, options)
        self.facts = facts or []
        self.qas = qas or []
        # 本地自检用站点根 URL 把相对路径补全，使"是否可被引用"的判定贴近真实
        raw = (bl.authority.website or bl.domain or "").strip()
        self.site_base = raw.rstrip("/") if raw else ""

    def _abs_url(self, uri: str) -> str:
        """把卡片里的相对路径补成站点绝对地址；已是 URL 或空则原样返回。"""
        if not uri:
            return ""
        if uri.startswith(("http://", "https://")):
            return uri
        if not self.site_base:
            return uri
        return f"{self.site_base}/{uri.lstrip('/')}"

    def fetch(self, query: str) -> Dict[str, Any]:
        terms = _terms(query)
        scored: List[Tuple[float, str, str]] = []
        for f in self.facts:
            text = f"{f.topic} {f.claim} {f.citable}"
            s = _overlap(terms, _terms(text))
            if s > 0:
                scored.append((s + f.score / 500, f.citable or f.claim, f.evidence_uri))
        for q in self.qas:
            s = _overlap(terms, _terms(f"{q.question} {q.answer}"))
            if s > 0:
                scored.append((s + 0.15 + q.score / 500, q.answer, q.evidence_uri))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:3]
        if not top:
            return {"answer": "", "citations": []}
        answer = " ".join(t[1] for t in top)
        citations = [self._abs_url(t[2]) for t in top if t[2]]
        return {"answer": answer, "citations": citations}


# ---------------------------------------------------------------- 生成式引擎探测

@REGISTRY.probe("openai_compat")
class OpenAICompatProbe(BaseProbe):
    """调用兼容 OpenAI 协议的生成式引擎接口。

    options:
        base_url: 如 https://api.perplexity.ai（Perplexity 会在返回中带 citations）
        model, api_key_env, timeout, system
        citations_field: 返回体中引用字段的路径，默认 "citations"
    """

    def fetch(self, query: str) -> Dict[str, Any]:
        base = self.options.get("base_url", "https://api.openai.com/v1").rstrip("/")
        api_key = os.getenv(self.options.get("api_key_env", "OPENAI_API_KEY"), "")
        if not api_key:
            raise RuntimeError(f"缺少 API Key（环境变量 {self.options.get('api_key_env')}）")
        payload = {
            "model": self.options.get("model", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": self.options.get(
                    "system", "请用中文回答，并在回答中给出你依据的信息来源链接。")},
                {"role": "user", "content": query},
            ],
            "temperature": float(self.options.get("temperature", 0.2)),
        }
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=int(self.options.get("timeout", 60))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations = _dig(data, self.options.get("citations_field", "citations")) or []
        if not citations:
            citations = _URL_RE.findall(answer)
        return {"answer": answer, "citations": list(citations)}


# ---------------------------------------------------------------- 搜索 API 探测

@REGISTRY.probe("search_api")
class SearchApiProbe(BaseProbe):
    """GET 一个搜索/问答 API，从结果中提取 URL 与位次。

    options:
        url:        含 {query} 占位符的地址模板
        headers:    请求头（支持 ${ENV} 注入）
        items_path: 结果列表路径，如 "organic" 或 "data.items"
        url_key:    每条结果里 URL 的字段名，默认 "link"
        title_key / snippet_key: 用于拼装 answer 文本
    """

    def fetch(self, query: str) -> Dict[str, Any]:
        tpl = self.options.get("url")
        if not tpl:
            raise RuntimeError("缺少 options.url")
        url = tpl.replace("{query}", urllib.parse.quote(query))
        headers = {}
        for k, v in (self.options.get("headers") or {}).items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                v = os.getenv(v[2:-1], "")
            headers[k] = v
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=int(self.options.get("timeout", 30))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = _dig(data, self.options.get("items_path", "")) if self.options.get("items_path") else data
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            items = []
        uk = self.options.get("url_key", "link")
        tk = self.options.get("title_key", "title")
        sk = self.options.get("snippet_key", "snippet")
        citations = [str(_dig(it, uk)) for it in items if _dig(it, uk)]
        answer = " ".join(
            f"{_dig(it, tk) or ''} {_dig(it, sk) or ''}" for it in items[:5]
        )
        return {"answer": answer, "citations": citations}


# ---------------------------------------------------------------- 编排

class Tracker:
    """按「引擎 × 问题」矩阵执行探测并落库。"""

    def __init__(self, bl: BusinessLine, store=None,
                 facts: Optional[List[FactCard]] = None,
                 qas: Optional[List[QAPair]] = None) -> None:
        self.bl = bl
        self.store = store
        self.facts = facts or []
        self.qas = qas or []

    def resolve_engine(self, name: str) -> Tuple[str, Dict[str, Any]]:
        """把配置里的引擎名解析成 (probe_type, options)。"""
        if ":" in name:
            engine, ptype = name.split(":", 1)
        else:
            engine, ptype = name, ""
        cfg = (self.bl.monitor.options.get("engines") or {}).get(engine, {})
        ptype = ptype or cfg.get("type") or self.bl.monitor.options.get("default_probe", "heuristic")
        return ptype, {"engine": engine, **cfg}

    def run(self, queries: Optional[List[str]] = None,
            engines: Optional[List[str]] = None) -> List[ProbeResult]:
        queries = queries or self.bl.monitor.queries
        engines = engines or self.bl.monitor.engines or ["local"]
        results: List[ProbeResult] = []
        for name in engines:
            ptype, opts = self.resolve_engine(name)
            engine = opts.get("engine", name)
            if not REGISTRY.has("probe", ptype):
                log.warning("[%s] 未注册的探测器: %s", self.bl.id, ptype)
                continue
            cls = REGISTRY.get("probe", ptype)
            extra = {"facts": self.facts, "qas": self.qas} if ptype == "heuristic" else {}
            try:
                probe = cls(self.bl, engine, opts, **extra)
            except TypeError:
                probe = cls(self.bl, engine, opts)
            for q in queries:
                results.append(probe.probe(q))
                _sleep(float(self.bl.monitor.options.get("qps_delay", 0)))
        if self.store and results:
            self.store.save_probes(results)
            log.info("[%s] 完成 %d 次探测并入库", self.bl.id, len(results))
        return results


# ---------------------------------------------------------------- 指标

class MetricsEngine:
    """由探测结果计算 GEO 核心指标。"""

    @staticmethod
    def compute(bl: BusinessLine, probes: List[ProbeResult],
                since_days: int = 30) -> MetricsSnapshot:
        if not probes:
            return MetricsSnapshot(business_line=bl.id)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        recent = [p for p in probes if (p.checked_at or "") >= cutoff] or probes

        total = len(recent)
        mention = sum(1 for p in recent if p.mentioned)
        citation = sum(1 for p in recent if p.cited)
        ranks = [p.rank for p in recent if p.rank > 0]
        competitors_hits = sum(len(p.competitors_mentioned or []) for p in recent)
        sov = citation / max(citation + competitors_hits, 1)
        sentiments = [p.sentiment for p in recent if p.sentiment]

        by_engine: Dict[str, Dict[str, float]] = {}
        for p in recent:
            e = by_engine.setdefault(p.engine, {"total": 0, "mention": 0, "citation": 0, "rank_sum": 0})
            e["total"] += 1
            e["mention"] += int(p.mentioned)
            e["citation"] += int(p.cited)
            e["rank_sum"] += p.rank or 0
        for e in by_engine.values():
            e["mention_rate"] = round(e["mention"] / max(e["total"], 1), 4)
            e["citation_rate"] = round(e["citation"] / max(e["total"], 1), 4)
            e["avg_rank"] = round(e["rank_sum"] / max(e["citation"], 1), 2)

        counters: Dict[str, Dict[str, int]] = {}
        for p in recent:
            c = counters.setdefault(p.query, {"cited": 0, "mentioned": 0, "total": 0})
            c["total"] += 1
            c["cited"] += int(p.cited)
            c["mentioned"] += int(p.mentioned)
        top_queries = sorted(
            ({"query": q, **v} for q, v in counters.items()),
            key=lambda x: (x["cited"], x["mentioned"]), reverse=True,
        )[:10]
        gaps = [q for q, v in counters.items() if v["cited"] == 0]

        return MetricsSnapshot(
            business_line=bl.id,
            total_queries=total,
            mention_count=mention,
            citation_count=citation,
            mention_rate=round(mention / max(total, 1), 4),
            citation_rate=round(citation / max(total, 1), 4),
            avg_rank=round(sum(ranks) / max(len(ranks), 1), 2),
            sov=round(sov, 4),
            sentiment=round(sum(sentiments) / max(len(sentiments), 1), 3),
            by_engine=by_engine,
            top_queries=top_queries,
            gaps=gaps,
        )

    @staticmethod
    def trend(bl: BusinessLine, probes: List[ProbeResult],
              window_days: int = 7) -> Dict[str, Any]:
        """对比最近一个窗口与上一个窗口的引用率变化。"""
        now = datetime.now(timezone.utc)
        cur_cut = (now - timedelta(days=window_days)).isoformat()
        prev_cut = (now - timedelta(days=window_days * 2)).isoformat()

        def rate(subset: List[ProbeResult]) -> float:
            if not subset:
                return 0.0
            return round(sum(1 for p in subset if p.cited) / len(subset), 4)

        cur = [p for p in probes if (p.checked_at or "") >= cur_cut]
        prev = [p for p in probes if prev_cut <= (p.checked_at or "") < cur_cut]
        r_cur, r_prev = rate(cur), rate(prev)
        return {
            "window_days": window_days,
            "current": r_cur,
            "previous": r_prev,
            "delta": round(r_cur - r_prev, 4),
            "direction": "up" if r_cur > r_prev else ("down" if r_cur < r_prev else "flat"),
            "samples": {"current": len(cur), "previous": len(prev)},
        }


# ---------------------------------------------------------------- 报表

class ReportBuilder:
    """生成 Markdown 报表与 HTML 看板。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl

    def markdown(self, snap: MetricsSnapshot, trend: Dict[str, Any],
                 coverage: Dict[str, Any] = None, counts: Dict[str, int] = None) -> str:
        pct = lambda x: f"{x * 100:.1f}%"
        L: List[str] = []
        L.append(f"# {self.bl.name} · GEO 效果报告")
        L.append("")
        L.append(f"- 业务线：`{self.bl.id}`")
        L.append(f"- 生成时间：{utcnow()}")
        L.append(f"- 统计样本：{snap.total_queries} 次探测")
        L.append("")
        L.append("## 核心指标")
        L.append("")
        L.append("| 指标 | 数值 | 说明 |")
        L.append("| --- | --- | --- |")
        L.append(f"| 提及率 | {pct(snap.mention_rate)} | 回答中出现品牌/产品的比例 |")
        L.append(f"| 引用率 | {pct(snap.citation_rate)} | 回答中引用本站域名的比例 |")
        L.append(f"| 平均引用位次 | {snap.avg_rank or '-'} | 越小越靠前（仅统计已引用） |")
        L.append(f"| 声量份额 SOV | {pct(snap.sov)} | 本站引用数 /（本站 + 竞品引用数） |")
        L.append(f"| 情感倾向 | {snap.sentiment:+.2f} | 区间 -1 ~ +1 |")
        L.append("")
        L.append("## 趋势")
        L.append("")
        L.append(f"- 近 {trend.get('window_days', 7)} 天引用率 **{pct(trend.get('current', 0))}**，"
                 f"上一周期 {pct(trend.get('previous', 0))}，"
                 f"变化 **{trend.get('delta', 0) * 100:+.1f} 个百分点**（{trend.get('direction')}）")
        L.append("")
        if snap.by_engine:
            L.append("## 分引擎表现")
            L.append("")
            L.append("| 引擎 | 样本 | 提及率 | 引用率 | 平均位次 |")
            L.append("| --- | --- | --- | --- | --- |")
            for e, v in snap.by_engine.items():
                L.append(f"| {e} | {int(v['total'])} | {pct(v['mention_rate'])} | "
                         f"{pct(v['citation_rate'])} | {v['avg_rank'] or '-'} |")
            L.append("")
        if snap.top_queries:
            L.append("## 表现最好的问题")
            L.append("")
            for item in snap.top_queries[:8]:
                L.append(f"- {item['query']}（引用 {item['cited']}/{item['total']}）")
            L.append("")
        if snap.gaps:
            L.append("## 待优化问题（尚未获得引用）")
            L.append("")
            for q in snap.gaps[:15]:
                L.append(f"- [ ] {q}")
            L.append("")
        if coverage:
            L.append("## 内容意图覆盖")
            L.append("")
            L.append(f"- 整体覆盖率：**{pct(coverage.get('coverage', 0))}**")
            have = coverage.get("have") or {}
            if have:
                L.append("- 现有分布：" + "、".join(f"{k} {v} 条" for k, v in have.items()))
            for g in coverage.get("gaps") or []:
                L.append(f"- 缺口意图：**{g['intent']}**（需要 {g['need']}，现有 {g['have']}）"
                         + "；建议补写：" + "；".join(g["suggest"][:2]))
            L.append("")
        if counts:
            L.append("## 知识资产规模")
            L.append("")
            L.append("| 类型 | 数量 |")
            L.append("| --- | --- |")
            names = {"documents": "原始文档", "chunks": "语义块", "facts": "事实卡",
                     "qas": "问答对", "terms": "术语", "artifacts": "发布产物"}
            for k, v in counts.items():
                if k in names:
                    L.append(f"| {names[k]} | {v} |")
            L.append("")
        L.append("---")
        L.append("> 由 GEO 引擎自动生成。指标口径：提及率=回答出现品牌的比例；"
                 "引用率=回答引用本站域名的比例。")
        return "\n".join(L)

    def html(self, snap: MetricsSnapshot, trend: Dict[str, Any],
             coverage: Dict[str, Any] = None, counts: Dict[str, int] = None) -> str:
        pct = lambda x: f"{x * 100:.1f}%"
        bars = "".join(
            f'<div class="row"><span class="label">{_e(e)}</span>'
            f'<span class="bar"><i style="width:{max(v["citation_rate"] * 100, 2):.1f}%"></i></span>'
            f'<span class="val">{pct(v["citation_rate"])}</span></div>'
            for e, v in (snap.by_engine or {}).items()
        )
        delta = trend.get("delta", 0) * 100
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        color = "#c0392b" if delta > 0 else ("#27865a" if delta < 0 else "#5b6570")
        gaps = "".join(f"<li>{_e(q)}</li>" for q in (snap.gaps or [])[:20]) or "<li>无</li>"
        tops = "".join(
            f"<li>{_e(i['query'])} <span class='meta'>引用 {i['cited']}/{i['total']}</span></li>"
            for i in (snap.top_queries or [])[:8]
        ) or "<li>暂无数据</li>"
        body = f"""
<header><h1>{_e(self.bl.name)} · GEO 看板</h1>
<p class="sub">生成时间 {utcnow()} · 样本 {snap.total_queries} 次探测</p></header>
<div class="grid">
  <div class="kpi"><b>{pct(snap.mention_rate)}</b><span>提及率</span></div>
  <div class="kpi"><b>{pct(snap.citation_rate)}</b><span>引用率</span></div>
  <div class="kpi"><b>{snap.avg_rank or '-'}</b><span>平均引用位次</span></div>
  <div class="kpi"><b>{pct(snap.sov)}</b><span>声量份额 SOV</span></div>
  <div class="kpi"><b>{snap.sentiment:+.2f}</b><span>情感倾向</span></div>
  <div class="kpi"><b style="color:{color}">{arrow} {abs(delta):.1f}pp</b><span>引用率环比</span></div>
</div>
<h2>分引擎引用率</h2>
<div class="bars">{bars or '<p class="meta">暂无数据</p>'}</div>
<h2>表现最好的问题</h2><ol>{tops}</ol>
<h2>待优化问题（未获引用）</h2><ul>{gaps}</ul>
<footer>由 GEO 引擎自动生成</footer>
"""
        css = """
:root{--fg:#1b1f24;--muted:#5b6570;--line:#e3e8ef;--accent:#0f7b6c;--soft:#f6f8fa}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--fg);font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.7}
.wrap{max-width:960px;margin:0 auto;padding:32px 20px 70px}
h1{font-size:26px;margin:0 0 6px}
h2{font-size:19px;margin:32px 0 12px;padding-left:10px;border-left:4px solid var(--accent)}
p.sub{color:var(--muted);margin:0 0 18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{border:1px solid var(--line);border-radius:10px;padding:14px 16px;background:var(--soft)}
.kpi b{display:block;font-size:24px;color:var(--accent)}
.kpi span{color:var(--muted);font-size:13px}
.bars .row{display:flex;align-items:center;gap:10px;margin:8px 0}
.bars .label{width:110px;font-size:14px;color:var(--muted)}
.bars .bar{flex:1;height:12px;background:var(--soft);border-radius:6px;overflow:hidden}
.bars .bar i{display:block;height:100%;background:var(--accent)}
.bars .val{width:60px;text-align:right;font-size:14px}
.meta{color:var(--muted);font-size:13px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
"""
        return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{_e(self.bl.name)} GEO 看板</title><style>{css}</style></head>'
                f'<body><div class="wrap">{body}</div></body></html>')


# ---------------------------------------------------------------- 工具

def _e(x: Any) -> str:
    import html as _h
    return _h.escape(str(x if x is not None else ""), quote=True)


def _domain_of(url: str) -> str:
    if not url:
        return ""
    m = re.match(r"https?://([^/\s]+)", url if "//" in url else "https://" + url)
    return (m.group(1).lower().replace("www.", "") if m else "")


def _sentiment(text: str) -> float:
    if not text:
        return 0.0
    pos = sum(text.count(w) for w in POSITIVE)
    neg = sum(text.count(w) for w in NEGATIVE)
    if pos + neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 3)


def _terms(text: str) -> List[str]:
    """中文按 2-gram 切分 + 英文单词，用于轻量相关性匹配。"""
    out: List[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        if len(seg) == 1:
            out.append(seg)
        else:
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    out.extend(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", text or ""))
    return out


def _overlap(a: List[str], b: List[str]) -> float:
    if not a or not b:
        return 0.0
    sb = set(b)
    hit = sum(1 for x in set(a) if x in sb)
    return hit / max(len(set(a)), 1)


def _dig(obj: Any, path: str) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _sleep(sec: float) -> None:
    if sec and sec > 0:
        import time
        time.sleep(sec)
