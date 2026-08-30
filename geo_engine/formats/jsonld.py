"""JSON-LD 结构化数据构建器。

schema.org 是生成式引擎做实体识别与事实抽取的重要信号源。
这里产出：Organization / WebSite / FAQPage / DefinedTermSet / ItemList / BreadcrumbList。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import BusinessLine, FactCard, QAPair, Term
from ..registry import REGISTRY
from .base import base_url_of, jsonld_script, url_join

_SCHEMA = "https://schema.org"


@REGISTRY.formatter("jsonld")
class JSONLDBuilder:
    """按业务线生成各类 JSON-LD 图。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl
        self.base = base_url_of(bl)

    # ---------------------------------------------------------------- 组织
    def organization(self) -> Dict[str, Any]:
        a = self.bl.authority
        node: Dict[str, Any] = {
            "@context": _SCHEMA,
            "@type": "Organization",
            "@id": url_join(self.base, "#organization"),
            "name": a.org_legal_name or self.bl.name,
            "url": self.base,
            "description": self.bl.description,
            "knowsAbout": self.bl.topics,
            "audience": {
                "@type": "Audience",
                "audienceType": "、".join(self.bl.audience) if self.bl.audience else "企业客户",
            },
        }
        if a.aliases:
            node["alternateName"] = a.aliases
        if a.industry:
            node["industry"] = a.industry
        if a.region:
            node["areaServed"] = {"@type": "Place", "name": a.region}
        if a.founded:
            node["foundingDate"] = a.founded
        if a.certifications:
            node["hasCredential"] = [
                {"@type": "EducationalOccupationalCredential", "credentialCategory": "certification",
                 "name": c} for c in a.certifications
            ]
        if a.awards:
            node["award"] = a.awards
        if a.standards:
            node["knowsLanguage"] = node.get("knowsLanguage", "zh-CN")
            node["subjectOf"] = [
                {"@type": "CreativeWork", "name": s, "about": "技术标准"} for s in a.standards
            ]
        if a.authors:
            node["employee"] = [
                {
                    "@type": "Person",
                    "name": p.get("name", ""),
                    "jobTitle": p.get("title", ""),
                    "hasCredential": {
                        "@type": "EducationalOccupationalCredential",
                        "credentialCategory": p.get("credential", ""),
                    } if p.get("credential") else None,
                } for p in a.authors
            ]
            for e in node["employee"]:
                if e.get("hasCredential") is None:
                    e.pop("hasCredential")
        same_as = self.bl.options.get("same_as") or []
        if same_as:
            node["sameAs"] = same_as
        return node

    def website(self) -> Dict[str, Any]:
        return {
            "@context": _SCHEMA,
            "@type": "WebSite",
            "@id": url_join(self.base, "#website"),
            "url": self.base,
            "name": self.bl.name or self.bl.authority.org_legal_name,
            "description": self.bl.description,
            "inLanguage": self.bl.language,
            "publisher": {"@id": url_join(self.base, "#organization")},
        }

    # ---------------------------------------------------------------- FAQ
    def faq(self, qas: List[QAPair]) -> Dict[str, Any]:
        return {
            "@context": _SCHEMA,
            "@type": "FAQPage",
            "@id": url_join(self.base, "faq.html#faq"),
            "name": f"{self.bl.name or ''}常见问题",
            "inLanguage": self.bl.language,
            "about": {"@id": url_join(self.base, "#organization")},
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": q.question,
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": q.answer,
                    },
                } for q in qas
            ],
        }

    # ---------------------------------------------------------------- 术语表
    def glossary(self, terms: List[Term]) -> Dict[str, Any]:
        return {
            "@context": _SCHEMA,
            "@type": "DefinedTermSet",
            "@id": url_join(self.base, "glossary.html#glossary"),
            "name": f"{self.bl.name or ''}术语表",
            "inLanguage": self.bl.language,
            "hasDefinedTerm": [
                {
                    "@type": "DefinedTerm",
                    "name": t.term,
                    "description": t.definition,
                    "termCode": t.id,
                    **({"alternateName": t.aliases} if t.aliases else {}),
                } for t in terms
            ],
        }

    # ---------------------------------------------------------------- 事实清单
    def fact_list(self, facts: List[FactCard]) -> Dict[str, Any]:
        """用 ItemList 暴露事实卡，便于引擎按条目抓取与引用。"""
        return {
            "@context": _SCHEMA,
            "@type": "ItemList",
            "@id": url_join(self.base, "knowledge.html#facts"),
            "name": f"{self.bl.name or ''}核心事实",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "item": {
                        "@type": "Statement",
                        "name": f.topic or "事实",
                        "text": f.citable or f.claim,
                        "about": f.entities or [],
                        "url": f.evidence_uri or "",
                    },
                } for i, f in enumerate(facts)
            ],
        }

    # ---------------------------------------------------------------- 面包屑
    def breadcrumb(self, trail: List[Dict[str, str]]) -> Dict[str, Any]:
        return {
            "@context": _SCHEMA,
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "name": t.get("name", ""),
                    "item": url_join(self.base, t.get("path", "")),
                } for i, t in enumerate(trail)
            ],
        }

    # ---------------------------------------------------------------- 汇总
    def all(self, facts: List[FactCard], qas: List[QAPair],
            terms: List[Term]) -> Dict[str, Dict[str, Any]]:
        return {
            "organization": self.organization(),
            "website": self.website(),
            "faq": self.faq(qas),
            "glossary": self.glossary(terms),
            "facts": self.fact_list(facts),
        }

    def to_script(self, node: Dict[str, Any]) -> str:
        return jsonld_script(node)
