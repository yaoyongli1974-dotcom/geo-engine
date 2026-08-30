"""核心数据模型。

所有模型均为 dataclass，支持 to_dict/from_dict，便于 JSON 序列化、落库与跨模块传递。
新增字段时保持默认值，保证向后兼容（历史配置/数据仍可加载）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- 工具函数

_CJK = re.compile(r"[\u4e00-\u9fff]")


def utcnow() -> str:
    """返回 UTC ISO8601 时间戳（秒级，带时区）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def slugify(text: str, max_len: int = 60) -> str:
    """生成 URL/文件名友好的 slug（保留中日韩字符）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (text or "").strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len] or "item")


def estimate_tokens(text: str) -> int:
    """粗估 token 数：中文按 1 字 ≈ 1 token，其余按 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return int(cjk + (len(text) - cjk) / 4) + 1


def split_sentences(text: str) -> List[str]:
    """句子切分。

    关键点：英文句点夹在数字之间时（如 99.9%、0.05 m）不当作句末，
    否则会把技术文档里的数值拦腰截断，生成不可引用的碎片。
    """
    out: List[str] = []
    buf: List[str] = []
    n = len(text or "")
    for i, ch in enumerate(text or ""):
        buf.append(ch)
        if ch in "。！？；!?;":
            out.append("".join(buf))
            buf = []
        elif ch == ".":
            prev_digit = i > 0 and text[i - 1].isdigit()
            next_digit = i + 1 < n and text[i + 1].isdigit()
            if not (prev_digit and next_digit):
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


# ---------------------------------------------------------------- 基类

class DataModel:
    """提供 to_dict / from_dict 的通用能力。"""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]
            val = getattr(self, f.name)
            out[f.name] = _serialize(val)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        if not is_dataclass(cls):
            raise TypeError(f"{cls} 不是 dataclass")
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**kwargs)  # type: ignore[call-arg]

    def to_json(self, ensure_ascii: bool = False) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=ensure_ascii, indent=2)


def _serialize(val: Any) -> Any:
    if is_dataclass(val) and not isinstance(val, type):
        return {f.name: _serialize(getattr(val, f.name)) for f in fields(val)}
    if isinstance(val, dict):
        return {k: _serialize(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_serialize(v) for v in val]
    return val


# ---------------------------------------------------------------- 配置模型

@dataclass
class SourceConfig(DataModel):
    """一个内容来源。type 决定使用哪个 Reader（见 geo_engine.ingest）。"""

    type: str = "markdown_dir"          # markdown_dir | file | csv | jsonl | text | url | api
    path: str = ""                      # 目录/文件/URL，取决于 type
    tags: List[str] = field(default_factory=list)
    authority: int = 2                  # 1~5，来源权威度（越高越容易被引用）
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthorityConfig(DataModel):
    """权威信息配置 —— 用于给内容打上 E-E-A-T 信号。"""

    org_legal_name: str = ""
    aliases: List[str] = field(default_factory=list)   # 别名/简称，帮助 AI 做实体对齐
    website: str = ""
    industry: str = ""
    region: str = ""
    founded: str = ""
    certifications: List[str] = field(default_factory=list)  # 资质证书
    standards: List[str] = field(default_factory=list)       # 参与/遵循的标准
    awards: List[str] = field(default_factory=list)
    authors: List[Dict[str, Any]] = field(default_factory=list)        # name/title/credential
    evidence_base: List[Dict[str, Any]] = field(default_factory=list)  # 案例/报告/白皮书


@dataclass
class TargetConfig(DataModel):
    """一个分发目标。type 决定使用哪个 Publisher（见 geo_engine.distribute）。"""

    id: str = ""
    type: str = "local_static"          # local_static | git | http | indexnow | noop
    enabled: bool = True
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMConfig(DataModel):
    """LLM Provider 配置。provider=heuristic 时不联网，用于离线跑通全链路。"""

    provider: str = "heuristic"         # heuristic | openai_compat
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 1200
    timeout: int = 60


@dataclass
class MonitorConfig(DataModel):
    """监测配置。"""

    engines: List[str] = field(default_factory=lambda: ["generic"])
    queries: List[str] = field(default_factory=list)
    competitors: List[str] = field(default_factory=list)
    interval_hours: int = 24
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BusinessLine(DataModel):
    """业务线 —— 系统中一切数据的作用域。多业务线即多份此配置。"""

    id: str = ""
    name: str = ""
    description: str = ""
    domain: str = ""                    # 主域名（用于 AI 引用归属判定）
    language: str = "zh-CN"
    topics: List[str] = field(default_factory=list)        # 核心主题词
    audience: List[str] = field(default_factory=list)      # 目标人群
    competitors: List[str] = field(default_factory=list)   # 竞品域名/品牌
    entity: Dict[str, Any] = field(default_factory=dict)   # 组织实体附加字段
    authority: AuthorityConfig = field(default_factory=AuthorityConfig)
    sources: List[SourceConfig] = field(default_factory=list)
    targets: List[TargetConfig] = field(default_factory=list)
    llm: LLMConfig = field(default_factory=LLMConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    options: Dict[str, Any] = field(default_factory=dict)

    @property
    def safe_id(self) -> str:
        return slugify(self.id or self.name or "default")


# ---------------------------------------------------------------- 内容模型

@dataclass
class SourceDoc(DataModel):
    """接入的原始文档。"""

    id: str = ""
    business_line: str = ""
    title: str = ""
    content: str = ""
    source_type: str = "text"
    source_uri: str = ""
    lang: str = "zh-CN"
    authority: int = 2
    tags: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self):
        if not self.id:
            self.id = sha1(f"{self.business_line}|{self.source_uri}|{self.title}")[:16]


@dataclass
class Chunk(DataModel):
    """语义分块结果 —— 检索与引用的最小单元。"""

    id: str = ""
    doc_id: str = ""
    business_line: str = ""
    heading_path: str = ""              # 如 "产品 > 综合布线 > 六类线缆"
    text: str = ""
    tokens: int = 0
    entities: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    score: float = 0.0                  # 0~100 质量分
    score_detail: Dict[str, float] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self):
        if not self.id:
            self.id = sha1(f"{self.doc_id}|{self.heading_path}|{self.text}")[:16]
        if not self.tokens:
            self.tokens = estimate_tokens(self.text)


@dataclass
class FactCard(DataModel):
    """事实卡 —— 一条「可被 AI 直接引用」的自足陈述。"""

    id: str = ""
    business_line: str = ""
    topic: str = ""
    claim: str = ""                     # 自足陈述（含主体+数值+条件+时间）
    citable: str = ""                   # 一句话可引用版本（≤ 60 字，首选被引用）
    evidence: str = ""                  # 原文佐证
    evidence_uri: str = ""
    numbers: List[Dict[str, Any]] = field(default_factory=list)  # {value,unit,metric,context}
    entities: List[str] = field(default_factory=list)
    authority_signals: List[str] = field(default_factory=list)
    confidence: float = 0.0             # 0~1
    score: float = 0.0                  # 0~100
    lang: str = "zh-CN"
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self):
        if not self.id:
            self.id = sha1(f"{self.business_line}|{self.topic}|{self.claim}")[:16]


@dataclass
class QAPair(DataModel):
    """问答对 —— 对齐生成式引擎的问答式检索形态。"""

    id: str = ""
    business_line: str = ""
    question: str = ""
    answer: str = ""
    intent: str = "informational"       # informational | commercial | navigational | transactional
    topic: str = ""
    entities: List[str] = field(default_factory=list)
    evidence: str = ""
    evidence_uri: str = ""
    authority_signals: List[str] = field(default_factory=list)
    score: float = 0.0
    lang: str = "zh-CN"
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self):
        if not self.id:
            self.id = sha1(f"{self.business_line}|{self.question}")[:16]


@dataclass
class Term(DataModel):
    """术语条目 —— 帮助 AI 建立企业专属概念与实体的稳定映射。"""

    id: str = ""
    business_line: str = ""
    term: str = ""
    definition: str = ""
    aliases: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
    source_uri: str = ""
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self):
        if not self.id:
            self.id = sha1(f"{self.business_line}|{self.term}")[:16]


@dataclass
class KnowledgeNode(DataModel):
    """知识图谱节点。"""

    id: str = ""
    label: str = ""
    type: str = "Concept"               # Organization | Product | Service | Concept | Metric | Standard
    aliases: List[str] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)
    business_line: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = slugify(f"{self.business_line}-{self.type}-{self.label}")


@dataclass
class KnowledgeEdge(DataModel):
    """知识图谱边。"""

    src: str = ""
    dst: str = ""
    rel: str = "relatedTo"              # offers | belongsTo | hasMetric | compliesWith | relatedTo
    weight: float = 1.0
    business_line: str = ""


@dataclass
class KnowledgeGraph(DataModel):
    nodes: List[KnowledgeNode] = field(default_factory=list)
    edges: List[KnowledgeEdge] = field(default_factory=list)

    def add_node(self, node: KnowledgeNode) -> None:
        if not any(n.id == node.id for n in self.nodes):
            self.nodes.append(node)

    def add_edge(self, edge: KnowledgeEdge) -> None:
        key = (edge.src, edge.dst, edge.rel)
        if not any((e.src, e.dst, e.rel) == key for e in self.edges):
            self.edges.append(edge)


# ---------------------------------------------------------------- 发布模型

@dataclass
class Artifact(DataModel):
    """发布产物 —— 一个待分发的文件。"""

    path: str = ""                      # 相对站点根目录的路径
    content: str = ""
    format: str = "text"                # llms_txt | jsonld | markdown | html | xml | json
    business_line: str = ""
    checksum: str = ""
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self):
        if not self.checksum:
            self.checksum = sha1(self.content)


@dataclass
class PublishResult(DataModel):
    """单个发布目标的执行结果。"""

    target_id: str = ""
    target_type: str = ""
    ok: bool = True
    published: int = 0
    skipped: int = 0
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    finished_at: str = field(default_factory=utcnow)


# ---------------------------------------------------------------- 监测模型

@dataclass
class ProbeResult(DataModel):
    """一次「引擎 × 问题」的探测结果。"""

    id: str = ""
    business_line: str = ""
    engine: str = ""
    query: str = ""
    mentioned: bool = False             # 回答中提到品牌/产品
    cited: bool = False                 # 回答中引用了本站域名
    cited_domains: List[str] = field(default_factory=list)
    competitors_mentioned: List[str] = field(default_factory=list)
    rank: int = 0                       # 引用位次（0=未引用，1=首个）
    sentiment: float = 0.0              # -1 ~ 1
    answer_snippet: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(default_factory=utcnow)

    def __post_init__(self):
        if not self.id:
            self.id = sha1(f"{self.business_line}|{self.engine}|{self.query}|{self.checked_at}")[:16]


@dataclass
class MetricsSnapshot(DataModel):
    """一次监测生成的指标快照。"""

    business_line: str = ""
    engine: str = "ALL"
    total_queries: int = 0
    mention_count: int = 0
    citation_count: int = 0
    mention_rate: float = 0.0           # 提及率
    citation_rate: float = 0.0          # 引用率
    avg_rank: float = 0.0               # 平均引用位次（仅统计已引用）
    sov: float = 0.0                    # Share of Voice：本站引用数 / (本站 + 竞品引用数)
    sentiment: float = 0.0
    by_engine: Dict[str, Dict[str, float]] = field(default_factory=dict)
    top_queries: List[Dict[str, Any]] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)   # 有曝光无引用的问题（优化机会）
    computed_at: str = field(default_factory=utcnow)
