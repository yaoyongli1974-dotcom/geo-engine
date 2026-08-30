"""GEO 引擎端到端冒烟测试（零依赖，纯标准库）。

运行：python -m tests.test_smoke  或  python geo_engine/cli.py --root . check
"""
import json
import os
import sys
import tempfile
import unittest

# 允许以脚本方式直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geo_engine import models as m
from geo_engine import chunker as ch
from geo_engine import structure as st
from geo_engine import semantic as se
from geo_engine import formats as fm
from geo_engine import monitor as mo
from geo_engine import pipeline as pl
from geo_engine import config as cf
from geo_engine import store as st_store

SAMPLE_MD = """# 示例智能科技有限公司 综合布线系统

## 线缆等级怎么选
六类线与超六类线的选择取决于带宽需求与传输距离：六类线支持 1Gbps 到 100 米，
超六类线支持 10GBase-T 到 100 米，关键指标 10GB。按 GB 50311 综合布线工程设计规范设计。

## 验收指标
系统应满足：链路永久链路测试通过率不低于 99.9%，端到端误码率低于 1e-9。
"""


def _doc(text: str = SAMPLE_MD, bl_id: str = "t") -> m.SourceDoc:
    return m.SourceDoc(id="s1", business_line=bl_id, title="样例", content=text,
                       source_type="text")


def _bl(bl_id: str = "t") -> m.BusinessLine:
    return m.BusinessLine(id=bl_id, name="测试", description="desc",
                          domain="example.com",
                          authority=m.AuthorityConfig(org_legal_name="测试公司",
                                                       website="https://example.com"))


class ModelTests(unittest.TestCase):
    def test_safe_id(self):
        bl = m.BusinessLine(id="弱电智能化", name="弱电", description="d")
        self.assertTrue(bl.safe_id)

    def test_slugify(self):
        self.assertEqual(m.slugify("GB 50311 规范"), "gb-50311-规范")
        self.assertEqual(m.slugify("Hello/World!"), "hello-world")

    def test_estimate_tokens_decimal(self):
        # 含小数点不应被错判为句末
        self.assertGreater(m.estimate_tokens("99.9% 指标"), 0)


class ChunkerTests(unittest.TestCase):
    def test_decimal_not_split(self):
        ck = ch.build_chunker("semantic")
        chunks = ck.chunk(_doc())
        joined = " ".join(c.text for c in chunks)
        self.assertIn("99.9%", joined)
        self.assertIn("10GB", joined)

    def test_chunk_has_tokens(self):
        ck = ch.build_chunker("semantic")
        chunks = ck.chunk(_doc())
        self.assertTrue(all(c.tokens > 0 for c in chunks))


class StructureTests(unittest.TestCase):
    def test_extract_facts_and_qa(self):
        bl = _bl()
        engine = st.StructureEngine(bl, None, use_llm=False)
        result = engine.process([_doc()])
        self.assertTrue(any("10GB" in f.claim or "99.9" in f.claim for f in result.facts))
        self.assertTrue(len(result.qas) >= 1)
        self.assertTrue(result.graph is not None and result.graph.nodes)


class SemanticTests(unittest.TestCase):
    def test_citable_enrich(self):
        bl = _bl()
        facts = [m.FactCard(claim="链路测试通过率不低于 99.9%", topic="验收指标",
                            numbers=[{"value": "99.9", "unit": "%"}])]
        enhancer = se.SemanticEnhancer(bl, None)
        out_facts, _, _, rep = enhancer.enhance(facts, [], [], [])
        self.assertIn("测试公司", out_facts[0].citable)
        self.assertIn("99.9", out_facts[0].citable)
        self.assertIsInstance(rep, se.EnhancementReport)


class FormatsTests(unittest.TestCase):
    def test_build_site_outputs(self):
        bl = _bl()
        facts = [m.FactCard(claim="x 不低于 99.9%", topic="通用",
                            numbers=[{"value": "99.9", "unit": "%"}])]
        qas = [m.QAPair(question="什么是 y？", answer="y 是 z。", intent="informational")]
        arts = fm.build_site(bl, facts, qas, [], None)
        names = {a.path for a in arts}
        self.assertIn("llms.txt", names)
        self.assertIn("llms-full.txt", names)
        self.assertIn("data/facts.jsonld", names)
        self.assertIn("data/faq.jsonld", names)
        self.assertTrue(any(a.path.endswith(".md") for a in arts))


class MonitorTests(unittest.TestCase):
    def test_heuristic_probe(self):
        bl = _bl()
        qas = [m.QAPair(question="综合布线怎么选？", answer="按带宽选。", intent="informational")]
        probe = mo.HeuristicProbe(bl, engine="local", facts=[], qas=qas)
        res = probe.probe("综合布线怎么选？")
        self.assertEqual(res.engine, "local")
        self.assertIsInstance(res.cited_domains, list)
        self.assertTrue(hasattr(res, "rank"))


class PipelineTests(unittest.TestCase):
    def test_end_to_end_memory(self):
        with tempfile.TemporaryDirectory() as d:
            bl_dir = os.path.join(d, "business_lines")
            os.makedirs(bl_dir, exist_ok=True)
            out_dir = os.path.join(d, "out")
            cfg = {
                "id": "t", "name": "测试", "description": "desc",
                "domain": "example.com",
                "authority": {"org_legal_name": "测试公司", "website": "https://example.com"},
                "sources": [{"type": "text", "options": {"content": SAMPLE_MD, "title": "样例"}}],
                "targets": [{"type": "local_static", "options": {"dir": out_dir}}],
                "llm": {"provider": "heuristic"},
                "monitor": {"engine": "heuristic", "queries": ["综合布线怎么选？"]},
            }
            with open(os.path.join(bl_dir, "t.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
            settings = cf.Settings(d, {})
            db = os.path.join(d, "geo.db")
            p = pl.GeoPipeline(settings, st_store.Store(db))
            res = p.run("t", force=True, use_llm=False)
            p.store.close()
            self.assertTrue(res.ok(), msg=str(res.errors))
            self.assertTrue(os.path.isdir(out_dir))
            self.assertTrue(any(a.path == "llms.txt" for a in res.artifacts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
