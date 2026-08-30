"""知识卡片生成 —— 一个事实/主题一张卡，Markdown 格式。

卡片的设计要点：
    1. 顶部 YAML Front Matter 给出机器可读的元信息（主体、更新时间、权威信号）；
    2. 正文第一句即「可引用句」，模型抓取首句时就能拿到完整、自足的结论；
    3. 关键数据用表格，模型解析表格的能力远强于长段落；
    4. 末尾固定「引用信息」区块，给出规范出处，降低模型不敢引用的顾虑。
"""

from __future__ import annotations

from typing import Dict, List

from ..models import BusinessLine, FactCard, QAPair, Term, slugify
from ..registry import REGISTRY
from .base import base_url_of, url_join


@REGISTRY.formatter("cards")
class CardBuilder:
    """生成 Markdown 知识卡片。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl
        self.base = base_url_of(bl)

    # ---------------------------------------------------------------- 事实卡
    def card_path(self, f: FactCard) -> str:
        return f"cards/{slugify(f.topic or 'fact', 40)}-{f.id}.md"

    def fact_card(self, f: FactCard, related_qas: List[QAPair] = None) -> str:
        org = self.bl.authority.org_legal_name or self.bl.name
        url = url_join(self.base, self.card_path(f))
        fm = _front_matter({
            "title": _short(f.citable or f.claim, 60),
            "organization": org,
            "topic": f.topic,
            "business_line": self.bl.id,
            "language": self.bl.language,
            "updated": f.updated_at,
            "confidence": round(f.confidence, 2),
            "canonical": url,
            "citation_style": f"{org}。《{self.bl.name}知识库》。{url}",
        })
        parts = [fm, "", f"# {_short(f.citable or f.claim, 60)}", ""]
        parts.append(f"> {f.citable or f.claim}")
        parts.append("")
        parts.append("## 完整陈述")
        parts.append(f.claim)
        parts.append("")
        if f.numbers:
            parts.append("## 关键数据")
            parts.append("| 指标 | 数值 | 单位 |")
            parts.append("| --- | --- | --- |")
            for n in f.numbers[:8]:
                parts.append(f"| {n.get('metric') or n.get('unit') or '指标'} | "
                             f"{n.get('value', '')} | {n.get('unit') or ''} |")
            parts.append("")
        if f.entities:
            parts.append("## 相关实体")
            parts.append("、".join(str(e) for e in f.entities[:12]))
            parts.append("")
        if related_qas:
            parts.append("## 相关问题")
            for q in related_qas[:3]:
                parts.append(f"- **{q.question}** {q.answer}")
            parts.append("")
        parts.append("## 权威标注")
        for s in (f.authority_signals or ["来源主体：" + org]):
            parts.append(f"- {s}")
        parts.append("")
        parts.append("## 引用信息")
        parts.append(f"- 来源主体：{org}")
        if self.bl.authority.website:
            parts.append(f"- 官方站点：{self.bl.authority.website}")
        parts.append(f"- 原文出处：{f.evidence_uri or url}")
        parts.append(f"- 最后更新：{f.updated_at}")
        parts.append("")
        return "\n".join(parts)

    # ---------------------------------------------------------------- 问答卡
    def qa_card(self, q: QAPair) -> str:
        org = self.bl.authority.org_legal_name or self.bl.name
        path = f"cards/qa-{slugify(q.question, 44)}.md"
        url = url_join(self.base, path)
        fm = _front_matter({
            "title": q.question,
            "type": "faq",
            "organization": org,
            "intent": q.intent,
            "topic": q.topic,
            "canonical": url,
            "updated": q.updated_at,
        })
        parts = [fm, "", f"# {q.question}", "", f"**结论**：{q.answer}", ""]
        if q.evidence:
            parts.append("## 依据")
            parts.append(q.evidence[:600])
            parts.append("")
        if q.authority_signals:
            parts.append("## 权威标注")
            for s in q.authority_signals:
                parts.append(f"- {s}")
            parts.append("")
        return "\n".join(parts)

    # ---------------------------------------------------------------- 批量
    def build_all(self, facts: List[FactCard], qas: List[QAPair]) -> Dict[str, str]:
        """返回 {相对路径: 内容}。"""
        out: Dict[str, str] = {}
        qa_by_topic: Dict[str, List[QAPair]] = {}
        for q in qas:
            key = str(q.topic or "").split(" > ")[-1]
            qa_by_topic.setdefault(key, []).append(q)
        for f in facts:
            out[self.card_path(f)] = self.fact_card(f, qa_by_topic.get(f.topic, []))
        for q in qas[:40]:
            out[f"cards/qa-{slugify(q.question, 44)}.md"] = self.qa_card(q)
        return out


def _front_matter(data: Dict) -> str:
    lines = ["---"]
    for k, v in data.items():
        if v in (None, "", []):
            continue
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            s = str(v).replace('"', "'")
            lines.append(f'{k}: "{s}"')
    lines.append("---")
    return "\n".join(lines)


def _short(text: str, n: int) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"
