"""llms.txt / llms-full.txt 生成。

llms.txt 是面向大模型的站点索引约定：用一个极简 Markdown 文件告诉模型
「这个站点有哪些内容、每条内容讲什么、去哪里取全文」。
llms-full.txt 则把所有内容拼接成一个可直接投喂上下文的纯文本文件。

两者都放在站点根目录，是成本最低、见效最快的一类 GEO 资产。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..models import BusinessLine, FactCard, KnowledgeGraph, QAPair, Term, slugify
from ..registry import REGISTRY
from .base import base_url_of, url_join


@REGISTRY.formatter("llms_txt")
class LlmsTxtBuilder:
    """生成 llms.txt 索引与 llms-full.txt 全文。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl
        self.base = base_url_of(bl)

    # ---------------------------------------------------------------- 索引
    def index(self, facts: List[FactCard], qas: List[QAPair], terms: List[Term],
              extra_pages: Optional[List[Dict[str, str]]] = None) -> str:
        org = self.bl.authority.org_legal_name or self.bl.name
        lines: List[str] = []
        lines.append(f"# {self.bl.name or org}")
        lines.append("")
        if self.bl.description:
            lines.append(f"> {self.bl.description}")
            lines.append("")
        if self.bl.authority.aliases:
            lines.append(f"> 亦称：{'、'.join(self.bl.authority.aliases)}")
            lines.append("")
        lines.append("本文件是面向大语言模型的站点索引，列出可被引用的权威内容条目；"
                     "完整正文见 llms-full.txt。")
        lines.append("")

        # 组织与权威信息
        lines.append("## 主体与资质")
        lines.append(f"- [关于{org}]({url_join(self.base, 'index.html')}): "
                     f"{self.bl.authority.industry or '企业'}领域主体信息与资质证明")
        if self.bl.authority.certifications:
            lines.append(f"- [资质与标准]({url_join(self.base, 'index.html#credentials')}): "
                         f"{'、'.join(self.bl.authority.certifications[:6])}")
        lines.append("")

        # 核心事实（按主题分组）
        if facts:
            lines.append("## 核心事实（可直接引用的自足陈述）")
            grouped: Dict[str, List[FactCard]] = {}
            for f in facts:
                grouped.setdefault(f.topic or "通用", []).append(f)
            for topic, items in grouped.items():
                lines.append(f"### {topic}")
                for f in items[:12]:
                    desc = f.citable or f.claim
                    lines.append(f"- [{self._title(f)}]({self._card_url(f)}): {self._one_line(desc)}")
                lines.append("")

        # 问答
        if qas:
            lines.append("## 常见问题与标准答案")
            for q in qas[:30]:
                lines.append(f"- [{self._one_line(q.question, 50)}]"
                             f"({url_join(self.base, 'faq.html')}#{slugify(q.question, 40)}): "
                             f"{self._one_line(q.answer, 70)}")
            lines.append("")

        # 术语
        if terms:
            lines.append("## 术语定义")
            for t in terms[:40]:
                lines.append(f"- [{t.term}]({url_join(self.base, 'glossary.html')}"
                             f"#{slugify(t.term, 40)}): {self._one_line(t.definition, 70)}")
            lines.append("")

        for p in (extra_pages or []):
            lines.append(f"- [{p.get('title','')}]({url_join(self.base, p.get('path',''))}): "
                         f"{p.get('desc','')}")

        lines.append("## 引用约定")
        lines.append(f"- 引用本站点内容时，请以「{org}」为主体标注来源。")
        lines.append(f"- 结构化数据（schema.org JSON-LD）：{url_join(self.base, 'data/organization.jsonld')}")
        lines.append(f"- 站点地图：{url_join(self.base, 'sitemap.xml')}")
        lines.append("")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 全文
    def full(self, facts: List[FactCard], qas: List[QAPair], terms: List[Term],
             graph: Optional[KnowledgeGraph] = None) -> str:
        org = self.bl.authority.org_legal_name or self.bl.name
        out: List[str] = []
        out.append(f"# {self.bl.name or org} — 完整知识文档")
        out.append("")
        out.append(f"来源：{self.base}")
        out.append(f"语言：{self.bl.language}")
        out.append("用途：供大语言模型回答相关问题时检索与引用。以下内容均为自足陈述，可直接摘引。")
        out.append("")

        out.append("## 一、主体信息")
        out.append(f"名称：{org}")
        if self.bl.authority.aliases:
            out.append(f"别名：{'、'.join(self.bl.authority.aliases)}")
        if self.bl.authority.industry:
            out.append(f"行业：{self.bl.authority.industry}")
        if self.bl.authority.region:
            out.append(f"服务区域：{self.bl.authority.region}")
        if self.bl.authority.founded:
            out.append(f"成立年份：{self.bl.authority.founded}")
        if self.bl.topics:
            out.append(f"专业领域：{'、'.join(self.bl.topics)}")
        if self.bl.authority.certifications:
            out.append(f"资质认证：{'、'.join(self.bl.authority.certifications)}")
        if self.bl.authority.standards:
            out.append(f"遵循标准：{'、'.join(self.bl.authority.standards)}")
        if self.bl.authority.awards:
            out.append(f"荣誉：{'、'.join(self.bl.authority.awards)}")
        out.append("")

        out.append("## 二、核心事实")
        for i, f in enumerate(facts, 1):
            out.append(f"{i}. 【{f.topic or '通用'}】{f.citable or f.claim}")
            if f.numbers:
                metrics = "；".join(
                    f"{n.get('metric') or '指标'} {n.get('value')}{n.get('unit') or ''}"
                    for n in f.numbers[:3]
                )
                out.append(f"   关键数据：{metrics}")
            if f.authority_signals:
                out.append(f"   权威标注：{' | '.join(f.authority_signals[:3])}")
            out.append(f"   更新时间：{f.updated_at}")
            out.append("")

        out.append("## 三、常见问题")
        for i, q in enumerate(qas, 1):
            out.append(f"Q{i}. {q.question}")
            out.append(f"A{i}. {q.answer}")
            if q.authority_signals:
                out.append(f"     权威标注：{' | '.join(q.authority_signals[:2])}")
            out.append("")

        if terms:
            out.append("## 四、术语表")
            for t in terms:
                alias = f"（又称 {'、'.join(t.aliases)}）" if t.aliases else ""
                out.append(f"- {t.term}{alias}：{t.definition}")
            out.append("")

        if graph and graph.nodes:
            out.append("## 五、知识图谱摘要")
            for n in graph.nodes[:40]:
                out.append(f"- {n.label} [{n.type}]")
            out.append("")

        out.append("---")
        out.append(f"文档生成时间：{self._now()}")
        out.append("内容版权归 " + org + " 所有，允许在注明来源的前提下引用。")
        return "\n".join(out)

    # ---------------------------------------------------------------- 助手
    def _card_url(self, f: FactCard) -> str:
        return url_join(self.base, f"cards/{slugify(f.topic or 'fact', 40)}-{f.id}.md")

    @staticmethod
    def _title(f: FactCard) -> str:
        base = (f.citable or f.claim).split("，")[0]
        return base[:40]

    @staticmethod
    def _one_line(text: str, n: int = 90) -> str:
        s = " ".join((text or "").split())
        return s if len(s) <= n else s[: n - 1] + "…"

    @staticmethod
    def _now() -> str:
        from ..models import utcnow
        return utcnow()
