"""端到端流水线编排。

阶段：
    ingest    接入   —— 读取各来源，产出 SourceDoc
    structure 整理   —— 分块、抽取实体/事实/问答/术语、建图谱、质量评分
    enhance   增强   —— 可引用化、权威标注、答案优先、实体对齐、意图覆盖分析
    build     构建   —— 渲染 llms.txt / JSON-LD / 知识卡片 / 静态站点
    publish   分发   —— 增量发布到各渠道
    monitor   监测   —— 多引擎探测，落库
    report    报表   —— 计算指标并生成 Markdown / HTML 报表

每个阶段可单独执行，也可一次跑通；阶段之间以 SQLite 为中介，便于断点续跑与审计。
"""

from __future__ import annotations

import os
import traceback
from typing import Any, Dict, List, Optional

from .config import ConfigRepository, Settings
from .distribute import DistributionManager
from .formats import build_site
from .ingest import run_ingest
from .llm import build_llm
from .logutil import get_logger
from .models import (
    Artifact,
    BusinessLine,
    FactCard,
    MetricsSnapshot,
    ProbeResult,
    QAPair,
    Term,
    utcnow,
)
from .monitor import MetricsEngine, ReportBuilder, Tracker
from .semantic import EnhancementReport, SemanticEnhancer
from .store import Store
from .structure import StructureEngine, StructureResult

log = get_logger("pipeline")

ALL_STAGES = ("ingest", "structure", "enhance", "build", "publish", "monitor", "report")


class PipelineResult:
    """一次运行的汇总结果。"""

    def __init__(self, bl_id: str) -> None:
        self.bl_id = bl_id
        self.stages: Dict[str, Dict[str, Any]] = {}
        self.artifacts: List[Artifact] = []
        self.publish = []
        self.metrics: Optional[MetricsSnapshot] = None
        self.errors: List[str] = []
        self.started_at = utcnow()
        self.finished_at = ""

    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bl_id": self.bl_id,
            "ok": self.ok(),
            "stages": self.stages,
            "artifacts": len(self.artifacts),
            "publish": [p.to_dict() for p in self.publish],
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "errors": self.errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class GeoPipeline:
    """GEO 主流水线。"""

    def __init__(self, settings: Settings, store: Optional[Store] = None) -> None:
        self.settings = settings
        self.repo = ConfigRepository(settings)
        self.store = store or Store(settings.db_path)

    # ---------------------------------------------------------------- 主入口
    def run(self, bl_id: str, stages: Optional[List[str]] = None,
            force: bool = False, use_llm: bool = True) -> PipelineResult:
        stages = stages or list(ALL_STAGES)
        bl = self.repo.load(bl_id)
        self.settings.ensure_dirs(bl.id)
        res = PipelineResult(bl.id)

        docs = []
        struct: Optional[StructureResult] = None
        facts: List[FactCard] = []
        qas: List[QAPair] = []
        terms: List[Term] = []
        coverage: Dict[str, Any] = {}

        for stage in stages:
            started = utcnow()
            try:
                if stage == "ingest":
                    docs = run_ingest(bl, self.store, root=self.settings.root)
                    res.stages["ingest"] = {"documents": len(docs)}

                elif stage == "structure":
                    if not docs:
                        docs = self._load_docs_from_meta(bl)
                    llm = self._make_llm(bl, use_llm)
                    engine = StructureEngine(bl, llm, use_llm=use_llm)
                    struct = engine.process(docs)
                    self.store.save_chunks(struct.chunks)
                    self.store.save_facts(struct.facts)
                    self.store.save_qas(struct.qas)
                    self.store.save_terms(struct.terms)
                    facts, qas, terms = struct.facts, struct.qas, struct.terms
                    res.stages["structure"] = struct.summary()

                elif stage == "enhance":
                    if struct is None:
                        facts = _facts_from_dicts(self.store.load_facts(bl.id))
                        qas = _qas_from_dicts(self.store.load_qas(bl.id))
                        terms = _terms_from_dicts(self.store.load_terms(bl.id))
                    llm = self._make_llm(bl, use_llm)
                    enhancer = SemanticEnhancer(bl, llm)
                    facts, qas, terms, rep = enhancer.enhance(
                        facts, qas, terms, bl.monitor.queries
                    )
                    coverage = rep.coverage
                    self.store.save_facts(facts)
                    self.store.save_qas(qas)
                    self.store.save_terms(terms)
                    res.stages["enhance"] = {
                        "facts": len(facts), "qas": len(qas), "terms": len(terms),
                        "stale": rep.stale_items,
                        "coverage": coverage.get("coverage", 0),
                        "notes": rep.notes,
                    }

                elif stage == "build":
                    if not facts:
                        facts = _facts_from_dicts(self.store.load_facts(bl.id))
                        qas = _qas_from_dicts(self.store.load_qas(bl.id))
                        terms = _terms_from_dicts(self.store.load_terms(bl.id))
                    graph = struct.graph if struct else None
                    artifacts = build_site(bl, facts, qas, terms, graph)
                    res.artifacts = artifacts
                    res.stages["build"] = {"artifacts": len(artifacts)}

                elif stage == "publish":
                    if not res.artifacts:
                        res.stages["publish"] = {"skipped": "无产物，先执行 build"}
                    else:
                        dm = DistributionManager(bl, self.store, root=self.settings.root)
                        results = dm.run(res.artifacts, force=force)
                        res.publish = results
                        res.stages["publish"] = {
                            "targets": len(results),
                            "ok": sum(1 for r in results if r.ok),
                            "failed": [r.message for r in results if not r.ok],
                        }

                elif stage == "monitor":
                    if not facts:
                        facts = _facts_from_dicts(self.store.load_facts(bl.id))
                        qas = _qas_from_dicts(self.store.load_qas(bl.id))
                    tracker = Tracker(bl, self.store, facts, qas)
                    probes = tracker.run()
                    res.stages["monitor"] = {"probes": len(probes)}

                elif stage == "report":
                    probes = [ProbeResult.from_dict(p) for p in self.store.load_probes(bl.id)]
                    snap = MetricsEngine.compute(bl, probes)
                    trend = MetricsEngine.trend(bl, probes)
                    rb = ReportBuilder(bl)
                    counts = self.store.stats(bl.id)
                    md = rb.markdown(snap, trend, coverage or None, counts)
                    html = rb.html(snap, trend, coverage or None, counts)
                    out_dir = self.settings.report_dir(bl.id)
                    os.makedirs(out_dir, exist_ok=True)
                    stamp = utcnow()[:19].replace(":", "-")
                    with open(os.path.join(out_dir, f"report-{stamp}.md"), "w",
                              encoding="utf-8") as f:
                        f.write(md)
                    with open(os.path.join(out_dir, "dashboard.html"), "w",
                              encoding="utf-8") as f:
                        f.write(html)
                    res.metrics = snap
                    res.stages["report"] = {
                        "citation_rate": snap.citation_rate,
                        "mention_rate": snap.mention_rate,
                        "sov": snap.sov,
                        "trend": trend.get("delta"),
                        "dir": out_dir,
                    }
                else:
                    res.errors.append(f"未知阶段: {stage}")
                    continue

            except Exception as exc:
                msg = f"阶段 {stage} 失败: {exc}"
                log.error("[%s] %s", bl.id, msg)
                log.debug(traceback.format_exc())
                res.errors.append(msg)
                res.stages[stage] = {"error": str(exc)}

            self.store.log_run(bl.id, stage,
                               "error" if stage in res.stages and isinstance(
                                   res.stages[stage], dict) and "error" in res.stages[stage] else "ok",
                               res.stages.get(stage, {}), started, utcnow())

        res.finished_at = utcnow()
        return res

    # ---------------------------------------------------------------- 批量
    def run_all(self, stages: Optional[List[str]] = None, force: bool = False,
                use_llm: bool = True) -> List[PipelineResult]:
        return [self.run(bl.id, stages, force, use_llm) for bl in self.repo.load_all()]

    # ---------------------------------------------------------------- 内部
    def _make_llm(self, bl: BusinessLine, use_llm: bool):
        if not use_llm or bl.llm.provider == "none":
            return None
        try:
            return build_llm(bl.llm)
        except Exception as exc:
            log.warning("[%s] LLM 初始化失败，回退离线模式: %s", bl.id, exc)
            return None

    def _load_docs_from_meta(self, bl: BusinessLine):
        """structure 阶段单独执行时，从库里恢复文档正文。"""
        import json
        out = []
        with self.store._lock:
            cur = self.store._conn.execute(
                "SELECT payload FROM documents WHERE business_line=?", (bl.id,)
            )
            for r in cur.fetchall():
                from .models import SourceDoc
                out.append(SourceDoc.from_dict(json.loads(r["payload"])))
        return out


# ---------------------------------------------------------------- 反序列化助手

def _facts_from_dicts(items: List[Dict[str, Any]]) -> List[FactCard]:
    return [FactCard.from_dict(x) for x in items]


def _qas_from_dicts(items: List[Dict[str, Any]]) -> List[QAPair]:
    return [QAPair.from_dict(x) for x in items]


def _terms_from_dicts(items: List[Dict[str, Any]]) -> List[Term]:
    return [Term.from_dict(x) for x in items]
