"""结构化整理 —— 分块 → 实体/事实/问答/术语抽取 → 知识图谱 → 质量评分。

输出的是「AI 易解析、易引用」的结构化资产，是整个 GEO 流水线的地基。
所有抽取器都遵循「LLM 优先、规则兜底」：有模型就用模型，没模型也能产出可用结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .chunker import build_chunker
from .llm import LLMProvider
from .models import (
    BusinessLine,
    Chunk,
    FactCard,
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeNode,
    QAPair,
    SourceDoc,
    Term,
    estimate_tokens,
    slugify,
    split_sentences,
    utcnow,
)
from .registry import REGISTRY

# ---------------------------------------------------------------- 提示词

PROMPT_FACTS = """你是企业知识工程师。请从下列材料中抽取「可被 AI 直接引用」的事实卡。

要求：
1. 每条事实必须是自足陈述：包含主体（企业/产品/系统）、关键数值或明确结论、适用条件、时间范围；
2. 禁止出现"该方案""此产品"等无指向代词，一律替换为具体名称「{org}」；
3. 优先抽取带数字、标准号、认证、对比结论、边界条件的句子；
4. 输出 JSON 数组，每条字段：claim（完整陈述）、citable（≤60 字的一句话引用版）、
   topic（主题）、numbers（[{{"value":..,"unit":..,"metric":..}}]）、entities（[..]）、confidence（0~1）。
5. 最多 6 条，宁缺毋滥。仅输出 JSON。

材料：
```
{text}
```"""

PROMPT_QA = """你是企业知识工程师。请基于下列材料生成问答对，用于对齐生成式搜索引擎的问答式检索。

要求：
1. 问题使用目标用户真实会问的口语表达，覆盖：是什么/怎么做/多少钱/选哪个/有什么标准/常见误区；
2. 答案以结论开头（答案优先），控制在 80~150 字，可独立成立，含必要数字与条件；
3. 输出 JSON 数组，字段：question、answer、intent（informational|commercial|navigational|transactional）、topic、entities；
4. 最多 5 条，不要编造材料中不存在的信息。仅输出 JSON。

材料：
```
{text}
```"""

PROMPT_TERMS = """你是术语编纂者。请从下列材料中抽取行业/企业专有术语，生成术语表条目。

输出 JSON 数组，字段：term（术语）、definition（40~80 字的准确定义）、aliases（别名/英文缩写数组）、related（相关术语数组）。
最多 8 条。仅输出 JSON。

材料：
```
{text}
```"""

# ---------------------------------------------------------------- 规则库

_NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|个百分点|米|m|cm|mm|千米|km|kg|千克|吨|"
                     r"元|万元|亿元|台|套|个|人|年|月|日|周|小时|分钟|天|次|度|kWh|W|kW|"
                     r"A|V|Mbps|Gbps|GHz|TB|GB|MB|dB|lux|lx|℃|分贝|级|倍)")
_STD_RE = re.compile(r"(GB[/ ]?\d+[\.\d\-]*|GB\s?\d+|ISO\s?\d+|IEC\s?\d+|IEEE\s?[\d\.]+|"
                     r"GA[/ ]?\d+|JGJ\s?\d+|YD[/ ]?\d+|SJ[/ ]?\d+|CE|CCC|FCC|RoHS|UL\s?\d+)")
_DATE_RE = re.compile(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})?|最新版|现行|截至\s*20\d{2}")
_VAGUE_RE = re.compile(r"^(该|此|这|其|它|上述|其中)")
_STOPWORDS = set("""的 了 和 与 及 在 是 为 对 从 到 有 无 不 也 都 就 而 或 等 中 上 下
可以 进行 通过 采用 使用 实现 提供 支持 具有 包括 以及 我们 公司 产品 系统 方案 服务
the a an and or of to in for with on is are be as by that this it""".split())


@dataclass
class StructureResult:
    """结构化整理的完整产物。"""

    chunks: List[Chunk] = field(default_factory=list)
    facts: List[FactCard] = field(default_factory=list)
    qas: List[QAPair] = field(default_factory=list)
    terms: List[Term] = field(default_factory=list)
    graph: KnowledgeGraph = field(default_factory=KnowledgeGraph)

    def summary(self) -> Dict[str, int]:
        return {
            "chunks": len(self.chunks),
            "facts": len(self.facts),
            "qas": len(self.qas),
            "terms": len(self.terms),
            "graph_nodes": len(self.graph.nodes),
            "graph_edges": len(self.graph.edges),
        }


# ---------------------------------------------------------------- 抽取器

class BaseExtractor:
    """抽取器基类：统一 LLM 调用 + 结果清洗。"""

    def __init__(self, bl: BusinessLine, llm: Optional[LLMProvider]) -> None:
        self.bl = bl
        self.llm = llm
        self.org = bl.authority.org_legal_name or bl.name or bl.id

    def _ask_json(self, prompt: str, default: Any) -> Any:
        if self.llm is None:
            return default
        try:
            return self.llm.complete_json(prompt)
        except Exception:
            return default


@REGISTRY.extractor("entity")
class EntityExtractor(BaseExtractor):
    """实体抽取：标准号、认证、产品/系统名、指标、组织别名。"""

    def extract(self, text: str) -> List[str]:
        ents: List[str] = []
        ents.extend(_STD_RE.findall(text))
        ents.extend(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,10}(?:系统|平台|方案|设备|线缆|模块|"
                               r"协议|规范|标准|认证|指标|传感器|控制器|摄像机|门禁|布线)", text))
        ents.extend(re.findall(r"\b[A-Z][A-Za-z0-9]*(?:[-/][A-Z0-9]+)*\b", text))
        out, seen = [], set()
        for e in ents:
            e = (e if isinstance(e, str) else "".join(e)).strip()
            if len(e) < 2 or e.lower() in _STOPWORDS or e in seen:
                continue
            seen.add(e)
            out.append(e)
        return out[:20]

    def keywords(self, text: str, topk: int = 12) -> List[str]:
        """无外部依赖的关键词提取：中文二/三字切分 + 词频。"""
        words = re.findall(r"[\u4e00-\u9fff]{2,4}|[A-Za-z][A-Za-z0-9\-]{2,}", text)
        freq: Dict[str, int] = {}
        for w in words:
            if w in _STOPWORDS:
                continue
            freq[w] = freq.get(w, 0) + 1
        # 长词优先（包含短词时短词降权）
        ranked = sorted(freq.items(), key=lambda kv: (-len(kv[0]) * 0.3 - kv[1], ))
        return [w for w, _ in ranked[:topk]]


@REGISTRY.extractor("fact")
class FactExtractor(BaseExtractor):
    """事实卡抽取。"""

    def extract(self, chunk: Chunk) -> List[FactCard]:
        raw = self._ask_json(
            PROMPT_FACTS.format(org=self.org, text=chunk.text[:3000]), None
        ) or self._rule_facts(chunk)
        cards: List[FactCard] = []
        for item in (raw if isinstance(raw, list) else []):
            claim = str(item.get("claim", "")).strip()
            if len(claim) < 10:
                continue
            citable = str(item.get("citable") or "").strip() or _shorten(claim, 60)
            card = FactCard(
                business_line=self.bl.id,
                topic=str(item.get("topic") or chunk.heading_path or "").split(" > ")[-1],
                claim=claim,
                citable=citable,
                evidence=chunk.text[:400],
                evidence_uri=chunk.meta.get("source_uri", ""),
                numbers=item.get("numbers") or [],
                entities=item.get("entities") or [],
                confidence=float(item.get("confidence") or 0.6),
                lang=self.bl.language,
            )
            card.score = QualityScorer().score_fact(card, chunk, self.bl)
            cards.append(card)
        return cards

    def _rule_facts(self, chunk: Chunk) -> List[Dict[str, Any]]:
        """规则兜底：挑信息密度最高的句子。"""
        out = []
        for sent in split_sentences(chunk.text):
            s = sent.strip()
            if len(s) < 12:
                continue
            nums = _NUM_RE.findall(s)
            stds = _STD_RE.findall(s)
            if not (nums or stds):
                continue
            out.append({
                "claim": _self_contained(s, self.org),
                "citable": _shorten(_self_contained(s, self.org), 60),
                "topic": chunk.heading_path.split(" > ")[-1] if chunk.heading_path else "",
                "numbers": [{"value": n, "unit": "", "metric": ""} for n in nums[:3]],
                "entities": stds[:3],
                "confidence": 0.6,
            })
            if len(out) >= 4:
                break
        return out


@REGISTRY.extractor("qa")
class QAExtractor(BaseExtractor):
    """问答对抽取。"""

    def extract(self, chunk: Chunk) -> List[QAPair]:
        raw = self._ask_json(PROMPT_QA.format(text=chunk.text[:3000]), None) or self._rule_qa(chunk)
        pairs: List[QAPair] = []
        for item in (raw if isinstance(raw, list) else []):
            q = str(item.get("question", "")).strip()
            a = str(item.get("answer", "")).strip()
            if len(q) < 5 or len(a) < 10:
                continue
            pair = QAPair(
                business_line=self.bl.id,
                question=q,
                answer=_ensure_answer_first(a),
                intent=str(item.get("intent") or "informational"),
                topic=str(item.get("topic") or chunk.heading_path or ""),
                entities=item.get("entities") or [],
                evidence=chunk.text[:300],
                evidence_uri=chunk.meta.get("source_uri", ""),
                lang=self.bl.language,
            )
            pair.score = QualityScorer().score_qa(pair)
            pairs.append(pair)
        return pairs

    def _rule_qa(self, chunk: Chunk) -> List[Dict[str, Any]]:
        title = (chunk.heading_path.split(" > ") or [""])[-1] or chunk.meta.get("title", "")
        if not title:
            return []
        body = re.sub(r"^.*?\n\n", "", chunk.text, flags=re.S).strip()
        answer = _shorten(body.replace("\n", " "), 150)
        if len(answer) < 15:
            return []
        return [{
            "question": f"{title} 是什么？",
            "answer": answer,
            "intent": "informational",
            "topic": title,
            "entities": [],
        }]


@REGISTRY.extractor("term")
class TermExtractor(BaseExtractor):
    """术语表抽取。"""

    def extract(self, chunk: Chunk) -> List[Term]:
        raw = self._ask_json(PROMPT_TERMS.format(text=chunk.text[:3000]), None) or self._rule_terms(chunk)
        out: List[Term] = []
        for item in (raw if isinstance(raw, list) else []):
            t = str(item.get("term", "")).strip()
            d = str(item.get("definition", "")).strip()
            if len(t) < 2 or len(d) < 8:
                continue
            out.append(Term(
                business_line=self.bl.id,
                term=t,
                definition=d,
                aliases=item.get("aliases") or [],
                related=item.get("related") or [],
                source_uri=chunk.meta.get("source_uri", ""),
            ))
        return out

    def _rule_terms(self, chunk: Chunk) -> List[Dict[str, Any]]:
        out = []
        for m in re.finditer(r"([^\s，。；：、（）()]{2,12})\s*(?:是指|定义为|即|（?简称|:\s)", chunk.text):
            term = m.group(1).strip()
            tail = chunk.text[m.end():m.end() + 120]
            defin = re.split(r"[。；\n]", tail)[0].strip()
            if len(defin) < 8:
                continue
            out.append({"term": term, "definition": defin[:120], "aliases": [], "related": []})
        return out[:5]


# ---------------------------------------------------------------- 质量评分

class QualityScorer:
    """0~100 质量分 —— 决定哪些内容有资格进入对外分发环节。

    维度：
      density  信息密度（数字/标准号/实体）
      clarity  自足与清晰（无悬空代词、句长适中）
      freshness 时效性（含时间/版本表述）
      authority 权威信号（来源权威度 + 标准/认证/资质）
      structure 结构度（标题/列表/表格）
    """

    W = {"density": 0.28, "clarity": 0.22, "authority": 0.22, "freshness": 0.13, "structure": 0.15}

    def score_chunk(self, chunk: Chunk, bl: BusinessLine) -> Tuple[float, Dict[str, float]]:
        text = chunk.text
        length = max(len(text), 1)
        nums = len(_NUM_RE.findall(text))
        stds = len(_STD_RE.findall(text))
        density = min(100.0, nums * 14 + stds * 18)

        sents = split_sentences(text)
        avg_len = (sum(len(s) for s in sents) / max(len(sents), 1)) if sents else 0
        vague = sum(1 for s in sents if _VAGUE_RE.match(s.strip()))
        clarity = 100.0
        clarity -= max(0, (avg_len - 60)) * 0.8                 # 句子过长扣分
        clarity -= (vague / max(len(sents), 1)) * 100 * 0.6     # 悬空代词扣分
        clarity -= 10 if not chunk.heading_path else 0
        clarity = max(0.0, min(100.0, clarity))

        freshness = 100.0 if _DATE_RE.search(text) else 45.0

        base_auth = {1: 40, 2: 55, 3: 68, 4: 80, 5: 92}.get(int(chunk.meta.get("authority", 2)), 55)
        authority = min(100.0, base_auth + stds * 6 + (8 if bl.authority.certifications else 0))

        structure = 40.0
        if chunk.heading_path:
            structure += 20
        if re.search(r"^\s*(?:[-*+]|\d+[.)])\s+", text, re.M):
            structure += 20
        if re.search(r"^\s*\|.*\|\s*$", text, re.M):
            structure += 20
        structure = min(100.0, structure)

        detail = {
            "density": round(density, 1),
            "clarity": round(clarity, 1),
            "freshness": round(freshness, 1),
            "authority": round(authority, 1),
            "structure": round(structure, 1),
        }
        total = sum(detail[k] * w for k, w in self.W.items())
        return round(total, 2), detail

    def score_fact(self, fact: FactCard, chunk: Chunk, bl: BusinessLine) -> float:
        s = 40.0
        if fact.numbers:
            s += 18
        if _STD_RE.search(fact.claim):
            s += 12
        if self.org_in_claim(fact.claim, bl):
            s += 12
        if _DATE_RE.search(fact.claim):
            s += 8
        if not _VAGUE_RE.match(fact.claim.strip()):
            s += 6
        s += min(10, len(fact.entities) * 2)
        s += (fact.confidence - 0.6) * 20
        return round(max(0.0, min(100.0, s)), 2)

    def score_qa(self, qa: QAPair) -> float:
        s = 40.0
        if 6 <= len(qa.question) <= 40:
            s += 15
        if 60 <= len(qa.answer) <= 200:
            s += 20
        elif len(qa.answer) > 20:
            s += 8
        if _NUM_RE.search(qa.answer):
            s += 12
        if _STD_RE.search(qa.answer):
            s += 8
        if qa.evidence:
            s += 5
        return round(max(0.0, min(100.0, s)), 2)

    @staticmethod
    def org_in_claim(claim: str, bl: BusinessLine) -> bool:
        names = [bl.authority.org_legal_name, bl.name, *bl.authority.aliases]
        return any(n and n in claim for n in names)


# ---------------------------------------------------------------- 知识图谱

class KnowledgeGraphBuilder:
    """由结构化资产构建轻量知识图谱（节点 + 关系），用于生成 JSON-LD 与 llms.txt 导航。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl

    def build(self, facts: List[FactCard], qas: List[QAPair],
              terms: List[Term], chunks: List[Chunk]) -> KnowledgeGraph:
        g = KnowledgeGraph()
        org = KnowledgeNode(
            label=self.bl.authority.org_legal_name or self.bl.name or self.bl.id,
            type="Organization",
            aliases=list(self.bl.authority.aliases),
            business_line=self.bl.id,
            properties={
                "url": self.bl.authority.website,
                "industry": self.bl.authority.industry,
                "region": self.bl.authority.region,
                "founded": self.bl.authority.founded,
            },
        )
        g.add_node(org)

        for t in self.bl.topics:
            topic_node = KnowledgeNode(label=t, type="Concept", business_line=self.bl.id)
            g.add_node(topic_node)
            g.add_edge(KnowledgeEdge(src=org.id, dst=topic_node.id, rel="covers",
                                     business_line=self.bl.id))

        for cert in self.bl.authority.certifications:
            n = KnowledgeNode(label=cert, type="Standard", business_line=self.bl.id)
            g.add_node(n)
            g.add_edge(KnowledgeEdge(src=org.id, dst=n.id, rel="compliesWith",
                                     business_line=self.bl.id))

        for f in facts:
            topic = f.topic or "通用"
            tn = KnowledgeNode(label=topic, type="Concept", business_line=self.bl.id)
            g.add_node(tn)
            g.add_edge(KnowledgeEdge(src=org.id, dst=tn.id, rel="hasKnowledge",
                                     business_line=self.bl.id, weight=max(0.1, f.score / 100)))
            for num in (f.numbers or [])[:2]:
                metric = str(num.get("metric") or num.get("unit") or "指标").strip()
                if not metric or len(metric) > 20:
                    continue
                mn = KnowledgeNode(label=metric, type="Metric", business_line=self.bl.id,
                                   properties={"unit": num.get("unit", "")})
                g.add_node(mn)
                g.add_edge(KnowledgeEdge(src=tn.id, dst=mn.id, rel="hasMetric",
                                         business_line=self.bl.id))
            for e in (f.entities or [])[:3]:
                if not e or len(e) > 24:
                    continue
                en = KnowledgeNode(label=str(e), type="Concept", business_line=self.bl.id)
                g.add_node(en)
                g.add_edge(KnowledgeEdge(src=tn.id, dst=en.id, rel="relatedTo",
                                         business_line=self.bl.id))

        for t in terms:
            tn = KnowledgeNode(label=t.term, type="Concept", aliases=list(t.aliases),
                               business_line=self.bl.id, properties={"definition": t.definition})
            g.add_node(tn)
            g.add_edge(KnowledgeEdge(src=org.id, dst=tn.id, rel="defines",
                                     business_line=self.bl.id))
            for r in (t.related or [])[:3]:
                rn = KnowledgeNode(label=str(r), type="Concept", business_line=self.bl.id)
                g.add_node(rn)
                g.add_edge(KnowledgeEdge(src=tn.id, dst=rn.id, rel="relatedTo",
                                         business_line=self.bl.id))

        for q in qas:
            if q.topic:
                tn = KnowledgeNode(label=str(q.topic).split(" > ")[-1], type="Concept",
                                   business_line=self.bl.id)
                g.add_node(tn)
                g.add_edge(KnowledgeEdge(src=org.id, dst=tn.id, rel="answersAbout",
                                         business_line=self.bl.id))
        return g


# ---------------------------------------------------------------- 编排

class StructureEngine:
    """结构化整理主流程。"""

    def __init__(self, bl: BusinessLine, llm: Optional[LLMProvider] = None,
                 chunker_name: str = "semantic",
                 max_chunks: int = 400,
                 use_llm: bool = True) -> None:
        self.bl = bl
        self.llm = llm if use_llm else None
        self.chunker = build_chunker(
            chunker_name,
            **(bl.options.get("chunker") or {})
        )
        self.max_chunks = max_chunks
        # 抽取器可替换：业务线配置 options.extractors = {"fact": "xxx"} 指定已注册的自定义实现
        override = bl.options.get("extractors") or {}
        self.entity_extractor = _pick("extractor", override.get("entity"), "entity", EntityExtractor)(bl, self.llm)
        self.fact_extractor = _pick("extractor", override.get("fact"), "fact", FactExtractor)(bl, self.llm)
        self.qa_extractor = _pick("extractor", override.get("qa"), "qa", QAExtractor)(bl, self.llm)
        self.term_extractor = _pick("extractor", override.get("term"), "term", TermExtractor)(bl, self.llm)
        self.scorer = QualityScorer()

    def process(self, docs: List[SourceDoc]) -> StructureResult:
        result = StructureResult()
        min_score = float(self.bl.options.get("min_chunk_score", 0))
        for doc in docs:
            for chunk in self.chunker.chunk(doc):
                chunk.entities = self.entity_extractor.extract(chunk.text)
                chunk.keywords = self.entity_extractor.keywords(chunk.text)
                chunk.score, chunk.score_detail = self.scorer.score_chunk(chunk, self.bl)
                if chunk.score < min_score:
                    continue
                result.chunks.append(chunk)

        # 只让高分块进入抽取，控制成本并提升质量
        ranked = sorted(result.chunks, key=lambda c: c.score, reverse=True)[: self.max_chunks]
        seen_fact, seen_q, seen_term = set(), set(), set()
        for chunk in ranked:
            for f in self.fact_extractor.extract(chunk):
                if f.claim in seen_fact:
                    continue
                seen_fact.add(f.claim)
                result.facts.append(f)
            for q in self.qa_extractor.extract(chunk):
                if q.question in seen_q:
                    continue
                seen_q.add(q.question)
                result.qas.append(q)
            for t in self.term_extractor.extract(chunk):
                if t.term in seen_term:
                    continue
                seen_term.add(t.term)
                result.terms.append(t)

        result.facts.sort(key=lambda x: x.score, reverse=True)
        result.qas.sort(key=lambda x: x.score, reverse=True)
        result.graph = KnowledgeGraphBuilder(self.bl).build(
            result.facts, result.qas, result.terms, result.chunks
        )
        return result


# ---------------------------------------------------------------- 文本工具

def _pick(category: str, name: Optional[str], default_name: str, default_cls):
    """优先取业务线指定的已注册实现，否则回退内置默认实现。"""
    key = name or default_name
    if REGISTRY.has(category, key):
        return REGISTRY.get(category, key)
    return default_cls


def _shorten(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[: n - 1].rstrip("，,、 ") + "…"


def _self_contained(sentence: str, org: str) -> str:
    """把悬空代词替换为组织名，让句子脱离原文也能被引用。"""
    s = sentence.strip()
    if _VAGUE_RE.match(s) and org:
        s = org + s[1:] if s[0] in "该此这其" else f"{org}的{s}"
    return s


def _ensure_answer_first(answer: str) -> str:
    """答案优先：把总结句提到最前（启发式，LLM 输出通常已符合）。"""
    sents = [s.strip() for s in re.split(r"(?<=[。！？])", answer or "") if s.strip()]
    if len(sents) <= 1:
        return _shorten(answer or "", 300)
    # 含数字/结论词的句子优先
    best = max(range(len(sents)), key=lambda i: (
        bool(_NUM_RE.search(sents[i])),
        any(k in sents[i] for k in ("应", "建议", "需要", "通常", "一般", "标准")),
        -i,
    ))
    if best == 0:
        return _shorten(answer, 300)
    rest = [s for i, s in enumerate(sents) if i != best]
    return _shorten(sents[best] + " " + " ".join(rest), 300)
