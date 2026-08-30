"""静态知识站点构建 —— 把所有标准化产物组装成一套可直接托管的网站。

产物清单：
    index.html            知识中心首页（Organization + WebSite JSON-LD）
    knowledge.html        核心事实清单（ItemList JSON-LD）
    faq.html              常见问题（FAQPage JSON-LD）
    glossary.html         术语表（DefinedTermSet JSON-LD）
    cards/*.md            知识卡片（Markdown，AI 最易解析的载体）
    llms.txt / llms-full.txt   面向大模型的索引与全文
    data/*.jsonld         结构化数据（可被任意引擎直接消费）
    data/knowledge-graph.json   知识图谱
    sitemap.xml / robots.txt / feed.xml   收录与更新信号
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional
from urllib.parse import quote

from ..models import (
    Artifact,
    BusinessLine,
    FactCard,
    KnowledgeGraph,
    QAPair,
    Term,
    slugify,
    utcnow,
)
from ..registry import REGISTRY
from .base import artifact, base_url_of, esc, jsonld_script, page, url_join
from .cards import CardBuilder
from .jsonld import JSONLDBuilder
from .llms_txt import LlmsTxtBuilder

# 主流生成式 AI / 搜索爬虫 UA（robots.txt 显式放行）
AI_CRAWLERS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Perplexity-User",
    "ClaudeBot", "Claude-User", "anthropic-ai", "Google-Extended", "Googlebot",
    "Bingbot", "Applebot", "Applebot-Extended", "Amazonbot", "CCBot", "YouBot",
    "Bytespider", "Baiduspider", "Sogou web spider", "DuckAssistBot", "Meta-ExternalAgent",
    "Diffbot", "Omgili", "Timpibot", "Webzio-Extended", "ImagesiftBot",
]


@REGISTRY.formatter("site")
class SiteBuilder:
    """一次构建，产出全部站点文件。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl
        self.base = base_url_of(bl)
        self.jsonld = JSONLDBuilder(bl)
        self.llms = LlmsTxtBuilder(bl)
        self.cards = CardBuilder(bl)

    # ---------------------------------------------------------------- 主页
    def build(self, facts: List[FactCard], qas: List[QAPair], terms: List[Term],
              graph: Optional[KnowledgeGraph] = None) -> List[Artifact]:
        bl = self.bl
        out: List[Artifact] = []
        org = bl.authority.org_legal_name or bl.name

        # ---- 1. 纯文本资产（AI 优先） ----
        out.append(artifact("llms.txt", self.llms.index(facts, qas, terms), "llms_txt", bl))
        out.append(artifact("llms-full.txt", self.llms.full(facts, qas, terms, graph), "llms_full", bl))

        # ---- 2. 知识卡片 ----
        for path, content in self.cards.build_all(facts, qas).items():
            out.append(artifact(path, content, "markdown", bl))

        # ---- 3. HTML 页面 ----
        out.append(artifact("index.html", self._index_page(facts, qas, terms), "html", bl))
        out.append(artifact("knowledge.html", self._knowledge_page(facts), "html", bl))
        out.append(artifact("faq.html", self._faq_page(qas), "html", bl))
        out.append(artifact("glossary.html", self._glossary_page(terms), "html", bl))

        # ---- 4. 结构化数据文件 ----
        ld = self.jsonld.all(facts, qas, terms)
        for key, node in ld.items():
            out.append(artifact(f"data/{key}.jsonld",
                                json.dumps(node, ensure_ascii=False, indent=2), "jsonld", bl))
        if graph:
            out.append(artifact("data/knowledge-graph.json",
                                json.dumps(graph.to_dict(), ensure_ascii=False, indent=2),
                                "json", bl))
        idx = {
            "business_line": bl.id,
            "organization": org,
            "updated_at": utcnow(),
            "counts": {"facts": len(facts), "qas": len(qas), "terms": len(terms)},
            "facts": [{"id": f.id, "topic": f.topic, "citable": f.citable,
                       "url": url_join(self.base, self.cards.card_path(f))} for f in facts],
            "qas": [{"id": q.id, "question": q.question, "intent": q.intent} for q in qas],
        }
        out.append(artifact("data/index.json", json.dumps(idx, ensure_ascii=False, indent=2),
                            "json", bl))

        # ---- 5. 收录与更新信号 ----
        paths = [a.path for a in out if a.path.endswith((".html", ".md", ".txt"))]
        out.append(artifact("sitemap.xml", self._sitemap(paths), "xml", bl))
        out.append(artifact("robots.txt", self._robots(), "text", bl))
        out.append(artifact("feed.xml", self._feed(facts, qas), "xml", bl))
        return out

    # ---------------------------------------------------------------- 页面
    def _index_page(self, facts: List[FactCard], qas: List[QAPair],
                    terms: List[Term]) -> str:
        bl = self.bl
        org = bl.authority.org_legal_name or bl.name
        a = bl.authority
        body: List[str] = []
        body.append("<header>")
        body.append(f"<h1>{esc(org)}</h1>")
        if bl.description:
            body.append(f'<p class="sub">{esc(bl.description)}</p>')
        if a.aliases:
            body.append(f'<p class="sub">亦称：{esc("、".join(a.aliases))}</p>')
        body.append("</header>")

        body.append('<nav class="toc"><strong>知识中心目录</strong><ul>')
        body.append('<li><a href="knowledge.html">核心事实（可直接引用）</a>'
                    f' — {len(facts)} 条</li>')
        body.append(f'<li><a href="faq.html">常见问题</a> — {len(qas)} 条</li>')
        body.append(f'<li><a href="glossary.html">术语表</a> — {len(terms)} 条</li>')
        body.append('<li><a href="llms.txt">llms.txt</a> / '
                    '<a href="llms-full.txt">llms-full.txt</a>（供大语言模型读取）</li>')
        body.append("</ul></nav>")

        body.append('<h2 id="about">主体概览</h2>')
        rows = [("名称", org), ("行业", a.industry), ("服务区域", a.region),
                ("成立年份", a.founded), ("官方站点", a.website)]
        body.append("<table>")
        for k, v in rows:
            if v:
                body.append(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>")
        if bl.topics:
            body.append(f"<tr><th>专业领域</th><td>{esc('、'.join(bl.topics))}</td></tr>")
        body.append("</table>")

        if a.certifications or a.standards or a.awards:
            body.append('<h2 id="credentials">资质与标准</h2>')
            if a.certifications:
                badges = "".join('<span class="badge">%s</span>' % esc(c) for c in a.certifications)
                body.append("<p>%s</p>" % badges)
            if a.standards:
                body.append(f"<p><strong>遵循标准：</strong>{esc('、'.join(a.standards))}</p>")
            if a.awards:
                body.append(f"<p><strong>荣誉：</strong>{esc('、'.join(a.awards))}</p>")

        if a.authors:
            body.append("<h2>内容审校</h2><ul>")
            for p in a.authors:
                body.append(f"<li>{esc(p.get('name',''))} · {esc(p.get('title',''))}"
                            f"{' · ' + esc(p.get('credential','')) if p.get('credential') else ''}</li>")
            body.append("</ul>")

        if facts:
            body.append("<h2>核心结论速览</h2>")
            for f in facts[:8]:
                body.append(f'<div class="card"><blockquote>{esc(f.citable or f.claim)}</blockquote>'
                            f'<div class="meta"><a href="{esc(self.cards.card_path(f))}">查看依据 →</a></div></div>')

        body.append(f'<footer>本页面为 {esc(org)} 的机器可读知识入口，'
                    f'内容同步提供 <a href="llms.txt">llms.txt</a> 与 JSON-LD 结构化数据。'
                    f'<br>最后更新：{esc(utcnow())}</footer>')

        head = jsonld_script(self.jsonld.organization()) + "\n" + jsonld_script(self.jsonld.website())
        return page(f"{org} — 知识中心", "\n".join(body),
                    description=bl.description, extra_head=head, bl=bl,
                    canonical=url_join(self.base, "index.html"))

    def _knowledge_page(self, facts: List[FactCard]) -> str:
        bl = self.bl
        org = bl.authority.org_legal_name or bl.name
        body: List[str] = ["<header>", f"<h1>{esc(org)} · 核心事实</h1>",
                           '<p class="sub">以下每条均为自足陈述，可独立引用；'
                           '点击卡片可查看原始依据。</p>', "</header>"]
        grouped: Dict[str, List[FactCard]] = {}
        for f in facts:
            grouped.setdefault(f.topic or "通用", []).append(f)
        for topic, items in grouped.items():
            body.append(f"<h2>{esc(topic)}</h2>")
            for f in items:
                body.append(f'<div class="card" id="{esc(f.id)}">')
                body.append(f"<h3>{esc(_shorten(f.citable or f.claim, 50))}</h3>")
                body.append(f"<blockquote>{esc(f.citable or f.claim)}</blockquote>")
                body.append(f"<p>{esc(f.claim)}</p>")
                if f.numbers:
                    body.append("<table><tr><th>指标</th><th>数值</th></tr>")
                    for n in f.numbers[:6]:
                        body.append(f"<tr><td>{esc(n.get('metric') or n.get('unit') or '指标')}</td>"
                                    f"<td>{esc(str(n.get('value','')) + (n.get('unit') or ''))}</td></tr>")
                    body.append("</table>")
                meta = [f"更新时间 {f.updated_at[:10]}", f"置信度 {f.confidence:.2f}"]
                body.append(f'<div class="meta">{esc(" · ".join(meta))} · '
                            f'<a href="{esc(self.cards.card_path(f))}">Markdown 卡片</a></div>')
                body.append("</div>")
        body.append(f'<footer><a href="index.html">← 返回知识中心</a></footer>')
        head = jsonld_script(self.jsonld.fact_list(facts))
        return page(f"{org} · 核心事实", "\n".join(body), bl=bl, extra_head=head,
                    canonical=url_join(self.base, "knowledge.html"))

    def _faq_page(self, qas: List[QAPair]) -> str:
        bl = self.bl
        org = bl.authority.org_legal_name or bl.name
        body: List[str] = ["<header>", f"<h1>{esc(org)} · 常见问题</h1>",
                           '<p class="sub">答案以结论开头，含关键数字与适用条件，可直接引用。</p>',
                           "</header>"]
        order = {"informational": 0, "commercial": 1, "transactional": 2, "navigational": 3}
        for q in sorted(qas, key=lambda x: order.get(x.intent, 9)):
            body.append(f'<div class="card" id="{esc(slugify(q.question, 40))}">')
            body.append(f"<h3>{esc(q.question)}</h3>")
            body.append(f"<p>{esc(q.answer)}</p>")
            body.append(f'<div class="meta">意图：{esc(q.intent)} · 更新：{esc(q.updated_at[:10])}</div>')
            body.append("</div>")
        body.append('<footer><a href="index.html">← 返回知识中心</a></footer>')
        head = jsonld_script(self.jsonld.faq(qas))
        return page(f"{org} · 常见问题", "\n".join(body), bl=bl, extra_head=head,
                    canonical=url_join(self.base, "faq.html"))

    def _glossary_page(self, terms: List[Term]) -> str:
        bl = self.bl
        org = bl.authority.org_legal_name or bl.name
        body: List[str] = ["<header>", f"<h1>{esc(org)} · 术语表</h1>", "</header>"]
        for t in sorted(terms, key=lambda x: x.term):
            alias = f"（又称 {esc('、'.join(t.aliases))}）" if t.aliases else ""
            body.append(f'<div class="card" id="{esc(slugify(t.term, 40))}">')
            body.append(f"<h3>{esc(t.term)}{alias}</h3>")
            body.append(f"<p>{esc(t.definition)}</p>")
            if t.related:
                rel = esc("、".join(map(str, t.related)))
                body.append('<div class="meta">相关：%s</div>' % rel)
            body.append("</div>")
        body.append('<footer><a href="index.html">← 返回知识中心</a></footer>')
        head = jsonld_script(self.jsonld.glossary(terms))
        return page(f"{org} · 术语表", "\n".join(body), bl=bl, extra_head=head,
                    canonical=url_join(self.base, "glossary.html"))

    # ---------------------------------------------------------------- 收录信号
    def _sitemap(self, paths: List[str]) -> str:
        urls = ["index.html", "knowledge.html", "faq.html", "glossary.html", "llms.txt"]
        urls += [p for p in paths if p not in urls]
        items = []
        today = utcnow()[:10]
        for p in dict.fromkeys(urls):
            items.append(
                f"  <url>\n    <loc>{esc(url_join(self.base, p))}</loc>\n"
                f"    <lastmod>{today}</lastmod>\n"
                f"    <changefreq>weekly</changefreq>\n    <priority>0.7</priority>\n  </url>"
            )
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + "\n".join(items) + "\n</urlset>\n")

    def _robots(self) -> str:
        lines = ["# robots.txt —— 显式放行主流生成式 AI 与搜索爬虫",
                 "User-agent: *",
                 "Allow: /",
                 ""]
        for ua in AI_CRAWLERS:
            lines.append(f"User-agent: {ua}")
            lines.append("Allow: /")
            lines.append("")
        lines.append(f"Sitemap: {url_join(self.base, 'sitemap.xml')}")
        lines.append("# 面向大模型的索引入口")
        lines.append(f"# llms.txt: {url_join(self.base, 'llms.txt')}")
        lines.append("")
        return "\n".join(lines)

    def _feed(self, facts: List[FactCard], qas: List[QAPair]) -> str:
        org = self.bl.authority.org_legal_name or self.bl.name
        items = []
        for f in facts[:20]:
            items.append(
                "  <item>\n"
                f"    <title>{esc(_shorten(f.citable or f.claim, 60))}</title>\n"
                f"    <link>{esc(url_join(self.base, self.cards.card_path(f)))}</link>\n"
                f"    <description>{esc(f.claim)}</description>\n"
                f"    <pubDate>{esc(f.updated_at)}</pubDate>\n"
                f"    <guid>{esc(f.id)}</guid>\n"
                "  </item>"
            )
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<rss version="2.0"><channel>\n'
                f"  <title>{esc(org)} 知识库更新</title>\n"
                f"  <link>{esc(self.base)}</link>\n"
                f"  <description>{esc(self.bl.description or org)}</description>\n"
                f"  <lastBuildDate>{esc(utcnow())}</lastBuildDate>\n"
                + "\n".join(items) + "\n</channel></rss>\n")


def build_site(bl: BusinessLine, facts: List[FactCard], qas: List[QAPair],
               terms: List[Term], graph: Optional[KnowledgeGraph] = None) -> List[Artifact]:
    """构建站点产物；可通过 options.formatter 指定已注册的自定义 formatter。"""
    name = (bl.options.get("formatter") or "site")
    cls = REGISTRY.get("formatter", name) if REGISTRY.has("formatter", name) else SiteBuilder
    return cls(bl).build(facts, qas, terms, graph)


def _shorten(text: str, n: int) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"
