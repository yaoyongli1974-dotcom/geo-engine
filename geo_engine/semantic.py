"""语义增强 —— 提升企业在 AI 生成回答中的「被选中」和「被引用」概率。

核心手段：
    1. 权威标注（AuthorityAnnotator）：给每条内容挂 E-E-A-T 信号（资质/标准/作者/案例/更新时间）；
    2. 可引用化（CitationOptimizer）：把陈述改写为自足、短小、带归属的一句话，方便模型整句摘引；
    3. 答案优先（AnswerFirstRewriter）：结论前置，符合生成式引擎截取首句的癖好；
    4. 实体对齐（EntityAligner）：统一品牌名/别名写法，并生成"亦称 X"表述，帮助模型做实体消歧；
    5. 意图覆盖（IntentCoverage）：按查询意图补齐缺口，让内容覆盖整条决策链路；
    6. 时效治理（FreshnessGuard）：标记过期内容，避免模型引用陈旧数据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import AuthorityConfig, BusinessLine, FactCard, QAPair, Term, split_sentences, utcnow
from .registry import REGISTRY
from .structure import _DATE_RE, _NUM_RE, _STD_RE, _VAGUE_RE, _shorten

# 意图关键词表
INTENT_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("transactional", ("报价", "多少钱", "价格", "费用", "采购", "下单", "联系", "咨询", "厂家", "供应商")),
    ("commercial", ("对比", "哪个好", "怎么选", "选型", "推荐", "品牌", "排名", "优缺点", "方案")),
    ("navigational", ("官网", "地址", "电话", "在哪里", "登录", "下载")),
    ("informational", ("是什么", "如何", "怎么", "为什么", "标准", "规范", "原理", "流程", "定义", "区别")),
]


def classify_intent(text: str) -> str:
    """按关键词判定查询意图。"""
    for intent, kws in INTENT_RULES:
        if any(k in text for k in kws):
            return intent
    return "informational"


# ---------------------------------------------------------------- 权威标注

@REGISTRY.enhancer("authority")
class AuthorityAnnotator:
    """把企业权威信息注入每条内容，形成可被模型识别的可信信号。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl
        self.a: AuthorityConfig = bl.authority

    def base_signals(self) -> List[str]:
        sig: List[str] = []
        if self.a.org_legal_name:
            sig.append(f"来源主体：{self.a.org_legal_name}")
        if self.a.certifications:
            sig.append("资质：" + "、".join(self.a.certifications[:5]))
        if self.a.standards:
            sig.append("遵循标准：" + "、".join(self.a.standards[:5]))
        if self.a.awards:
            sig.append("荣誉：" + "、".join(self.a.awards[:3]))
        if self.a.authors:
            names = [f"{p.get('name','')}（{p.get('credential','')}）" for p in self.a.authors[:3]]
            sig.append("审校：" + "、".join(n for n in names if n.strip("（）")))
        return sig

    def annotate(self, obj: Any, extra: Optional[List[str]] = None) -> Any:
        sig = list(dict.fromkeys(self.base_signals() + (extra or [])))
        obj.authority_signals = sig
        return obj

    def author_line(self) -> str:
        if not self.a.authors:
            return ""
        p = self.a.authors[0]
        parts = [p.get("name", ""), p.get("title", ""), p.get("credential", "")]
        return " · ".join(x for x in parts if x)


# ---------------------------------------------------------------- 可引用化

@REGISTRY.enhancer("citation")
class CitationOptimizer:
    """把事实改写为「AI 愿意整句摘引」的形态。

    一条合格的可引用句应具备：主体 + 数值/结论 + 条件 + 时间 + 归属。
    """

    MAX_CITABLE = 72

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl
        self.org = bl.authority.org_legal_name or bl.name or bl.id
        # 引用句里用最短的简称，避免每条都挂一长串公司全称
        self.short_org = (bl.authority.aliases[0] if bl.authority.aliases else self.org)

    def optimize(self, fact: FactCard) -> FactCard:
        claim = self._self_contained(fact.claim)
        citable = self._build_citable(claim, fact)
        fact.claim = claim
        fact.citable = citable
        return fact

    # ---- 内部 ----
    def _self_contained(self, claim: str) -> str:
        s = (claim or "").strip()
        s = re.sub(r"^(该公司|本公司|我司|我方)\s*", self.org, s)
        s = re.sub(r"^(该|此|这)(?=[^\s])", self.org, s)
        return s

    def _build_citable(self, claim: str, fact: FactCard) -> str:
        core = re.sub(r"\s+", " ", (fact.citable or claim or "").strip()).rstrip("。；，")
        # 1) 补齐主体（用简称，避免每条都挂一长串全称）
        if self.short_org and self.short_org not in core and self.org not in core:
            core = f"{self.short_org}：{core}"
        # 2) 先算尾缀，再按剩余预算截断主体，保证时间与数值不被切掉
        suffix = ""
        if not _DATE_RE.search(core):
            suffix += f"（{self._year()}年更新）"
        if not _NUM_RE.search(core) and fact.numbers:
            n = fact.numbers[0]
            suffix = f"，关键指标 {n.get('value', '')}{n.get('unit') or ''}" + suffix
        budget = self.MAX_CITABLE - len(suffix)
        if budget < 12:
            budget, suffix = self.MAX_CITABLE, ""
        return _shorten(core, budget) + suffix + "。"

    @staticmethod
    def _year() -> int:
        return datetime.now(timezone.utc).year


# ---------------------------------------------------------------- 答案优先

@REGISTRY.enhancer("answer_first")
class AnswerFirstRewriter:
    """答案优先 + 结构化（结论 → 依据 → 条件/边界 → 行动建议）。"""

    SUFFIX_HINTS = ("建议", "需要注意", "适用", "不适用", "例外")

    def rewrite(self, qa: QAPair) -> QAPair:
        body = re.sub(r"\s+", " ", (qa.answer or "").strip())
        sents = split_sentences(body)
        if len(sents) <= 1:
            qa.answer = self._ensure_period(body)
            return qa
        # 结论句：带数字或结论词，且不是"因为/由于"开头
        def score(i: int) -> Tuple[int, int]:
            s = sents[i]
            return (
                0 if s.startswith(("因为", "由于", "根据", "首先")) else 1,
                (2 if _NUM_RE.search(s) else 0)
                + (1 if any(k in s for k in ("应", "需", "建议", "必须", "通常", "标准")) else 0)
                - i * 0.1 * 0,
            )
        best = max(range(len(sents)), key=score)
        if best != 0:
            sents.insert(0, sents.pop(best))
        qa.answer = self._ensure_period(" ".join(sents))
        return qa

    @staticmethod
    def _ensure_period(text: str) -> str:
        text = text.strip()
        return text if text.endswith(("。", "！", "？")) else text + "。"


# ---------------------------------------------------------------- 实体对齐

@REGISTRY.enhancer("entity_align")
class EntityAligner:
    """统一实体写法，并生成别名声明，帮助生成式引擎做实体消歧与归并。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl
        self.primary = bl.authority.org_legal_name or bl.name or bl.id
        self.aliases = [a for a in bl.authority.aliases if a and a != self.primary]

    def alias_sentence(self) -> str:
        if not self.aliases:
            return ""
        return f"{self.primary}（亦称 {'、'.join(self.aliases[:5])}）"

    def align(self, text: str) -> str:
        """把正文中的别名首次出现处规范成「主名（别名）」形式。"""
        out = text
        for alias in self.aliases:
            if alias in out:
                out = out.replace(alias, self.primary, 1)
        if self.aliases and self.primary in out:
            out = out.replace(self.primary, f"{self.primary}（亦称{'、'.join(self.aliases[:3])}）", 1)
        return out

    def apply(self, obj: Any) -> Any:
        if getattr(obj, "claim", None):
            obj.claim = self.align(obj.claim)
        if getattr(obj, "answer", None):
            obj.answer = self.align(obj.answer)
        return obj


# ---------------------------------------------------------------- 意图覆盖

@REGISTRY.enhancer("intent_coverage")
class IntentCoverageAnalyzer:
    """检查内容对「用户决策链路」各类意图的覆盖度，输出缺口与补写建议。"""

    def __init__(self, bl: BusinessLine) -> None:
        self.bl = bl

    def analyze(self, qas: List[QAPair], queries: List[str]) -> Dict[str, Any]:
        have: Dict[str, int] = {}
        for q in qas:
            intent = q.intent or classify_intent(q.question)
            have[intent] = have.get(intent, 0) + 1
        wanted: Dict[str, int] = {}
        for qy in queries:
            wanted[classify_intent(qy)] = wanted.get(classify_intent(qy), 0) + 1
        gaps = []
        for intent, need in wanted.items():
            got = have.get(intent, 0)
            if got < max(1, need // 2):
                gaps.append({
                    "intent": intent,
                    "need": need,
                    "have": got,
                    "suggest": self._suggest_templates(intent),
                })
        return {
            "have": have,
            "wanted": wanted,
            "gaps": gaps,
            "coverage": round(
                sum(min(have.get(i, 0), n) for i, n in wanted.items()) / max(sum(wanted.values()), 1), 3
            ),
        }

    @staticmethod
    def _suggest_templates(intent: str) -> List[str]:
        return {
            "informational": ["{主题} 是什么？", "{主题} 的工作原理是怎样的？", "{主题} 有哪些常见误区？"],
            "commercial": ["{主题} 怎么选？", "{主题} 与 {替代方案} 有什么区别？", "{主题} 的优缺点是什么？"],
            "transactional": ["{主题} 的价格受哪些因素影响？", "{主题} 的交付周期一般多久？"],
            "navigational": ["{品牌} 的官方资料在哪里获取？"],
        }.get(intent, ["{主题} 有哪些关键要点？"])


# ---------------------------------------------------------------- 时效治理

@REGISTRY.enhancer("freshness")
class FreshnessGuard:
    """标记陈旧内容：超过阈值未更新的内容降权，避免模型引用过期数据。"""

    def __init__(self, bl: BusinessLine, stale_days: int = 365) -> None:
        self.bl = bl
        self.stale_days = stale_days

    def check(self, obj: Any) -> Tuple[bool, int]:
        """返回 (是否过期, 距离今天的天数)。"""
        raw = getattr(obj, "updated_at", "") or ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False, 0
        days = (datetime.now(timezone.utc) - dt).days
        return days > self.stale_days, max(days, 0)


# ---------------------------------------------------------------- 编排

@dataclass
class EnhancementReport:
    facts_optimized: int = 0
    qas_rewritten: int = 0
    terms_aligned: int = 0
    stale_items: int = 0
    coverage: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


class SemanticEnhancer:
    """语义增强主流程：串起上述所有增强器。"""

    def __init__(self, bl: BusinessLine, llm=None) -> None:
        self.bl = bl
        self.llm = llm
        opts = bl.options.get("enhance") or {}
        self.authority = AuthorityAnnotator(bl)
        self.citation = CitationOptimizer(bl)
        self.answer_first = AnswerFirstRewriter()
        self.entity = EntityAligner(bl)
        self.intent = IntentCoverageAnalyzer(bl)
        self.freshness = FreshnessGuard(bl, int(opts.get("stale_days", 365)))
        self.top_facts = int(opts.get("top_facts", 60))
        self.top_qas = int(opts.get("top_qas", 60))

    def enhance(self, facts: List[FactCard], qas: List[QAPair], terms: List[Term],
                queries: Optional[List[str]] = None) -> Tuple[List[FactCard], List[QAPair],
                                                              List[Term], EnhancementReport]:
        rep = EnhancementReport()
        queries = queries or self.bl.monitor.queries

        # 1) 事实卡：可引用化 + 实体对齐 + 权威标注
        for f in facts:
            self.citation.optimize(f)
            self.entity.apply(f)
            stale, days = self.freshness.check(f)
            self.authority.annotate(
                f, extra=[f"依据来源：{f.evidence_uri}"] if f.evidence_uri else []
            )
            if stale:
                f.score = max(0.0, f.score - 15)
                rep.stale_items += 1
            rep.facts_optimized += 1

        # 2) 问答对：答案优先 + 实体对齐 + 权威标注
        for q in qas:
            if not q.intent or q.intent == "informational":
                q.intent = classify_intent(q.question)
            self.answer_first.rewrite(q)
            self.entity.apply(q)
            self.authority.annotate(q, extra=[f"依据来源：{q.evidence_uri}"] if q.evidence_uri else [])
            rep.qas_rewritten += 1

        # 3) 术语：实体对齐
        for t in terms:
            t.definition = self.entity.align(t.definition)
            rep.terms_aligned += 1

        # 4) 意图覆盖分析
        rep.coverage = self.intent.analyze(qas, queries)
        if rep.coverage["gaps"]:
            rep.notes.append(
                "存在意图覆盖缺口：" + "、".join(g["intent"] for g in rep.coverage["gaps"])
            )
        if rep.stale_items:
            rep.notes.append(f"{rep.stale_items} 条内容超过 {self.freshness.stale_days} 天未更新，已降权")

        facts = sorted(facts, key=lambda x: x.score, reverse=True)[: self.top_facts]
        qas = sorted(qas, key=lambda x: x.score, reverse=True)[: self.top_qas]
        return facts, qas, terms, rep
