"""多租户隔离验证脚本（纯标准库，无需 fastapi）。

覆盖：
  1. 恶意 tenant_id / bl_id 被拒绝（路径穿越防护）
  2. 跨租户存储读隔离（A 的数据 B 读不到）
  3. 租户目录命名空间（root/content/dist 均落在 tenants/<tid> 下）
  4. 产物路径穿越拦截（serve_artifact 的同款 normpath 前缀校验）
  5. 并发写入（WAL + 锁，不抛 database is locked）
  6. OAuth 外部身份 → 租户映射（dev / 企业微信 test_mode）
  7. JWT 与口令哈希（签发/校验/错误密钥/错误口令）

运行：
    python -m tests.test_tenant_isolation
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest

# 必须在导入 geo_web 前确定数据根目录（模块在 import 时读取 GEO_DATA_DIR）
_TMP = tempfile.mkdtemp(prefix="geo_iso_test_")
os.environ["GEO_DATA_DIR"] = _TMP
os.environ["GEO_JWT_SECRET"] = "test-secret-only"

from geo_web.auth_providers import DevProvider, WeComProvider  # noqa: E402
from geo_web.control import control_store  # noqa: E402
from geo_web.security import (  # noqa: E402
    hash_password,
    sign_jwt,
    verify_jwt,
    verify_password,
)
from geo_web.tenant import (  # noqa: E402
    TenantContext,
    TenantStore,
    validate_id,
)
from geo_engine.models import SourceDoc  # noqa: E402


class TenantIsolationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cs = control_store()

    def test_validate_id_rejects_traversal(self):
        for bad in ["..", "../x", "/etc", "a/../b", "../../etc/passwd",
                    "", "a b", "a.b", "中文", "a" * 61]:
            with self.assertRaises(ValueError, msg=f"应拒绝非法 id: {bad!r}"):
                validate_id(bad, "tenant_id")
        # 合法
        self.assertEqual(validate_id("tenant_a1", "tenant_id"), "tenant_a1")
        self.assertEqual(validate_id("bl-2_x", "business_line"), "bl-2_x")

    def test_cross_tenant_store_isolation(self):
        ta = self.cs.create_tenant("租户A")
        tb = self.cs.create_tenant("租户B")
        sa = TenantStore(ta)
        sb = TenantStore(tb)

        doc = SourceDoc(business_line="bl1", title="机密文档",
                        content="A 的机密内容", source_type="text")
        sa.save_documents([doc])

        # A 有 1 篇，B 完全没有
        self.assertEqual(sa.stats("bl1")["documents"], 1)
        self.assertEqual(sb.stats("bl1")["documents"], 0)
        # 物理库路径隔离
        self.assertNotEqual(sa.db_path, sb.db_path)
        self.assertTrue(sa.db_path.endswith(os.path.join("tenants", ta, "geo.db")))

    def test_tenant_dir_namespacing(self):
        ta = self.cs.create_tenant("租户A")
        tc = TenantContext(ta)
        self.assertTrue(tc.root.endswith(os.path.join("tenants", ta)),
                        msg=f"root 未命名空间化: {tc.root}")
        for d in (tc.content_dir("bl1"), tc.dist_dir("bl1"), tc.report_dir("bl1")):
            self.assertTrue(d.startswith(tc.root), msg=f"子目录越出租户根: {d}")

    def test_path_traversal_in_artifact_path(self):
        ta = self.cs.create_tenant("租户A")
        tc = TenantContext(ta)
        base = os.path.normpath(tc.dist_dir("bl1"))
        for malicious in ["../../../../etc/passwd", "../secret", "/abs/etc",
                          "a/../../b"]:
            full = os.path.normpath(os.path.join(base, malicious))
            inside = full.startswith(base + os.sep) or full == base
            self.assertFalse(inside, f"路径穿越未被拦截: {malicious} -> {full}")

    def test_concurrent_writes_wal(self):
        ta = self.cs.create_tenant("租户A")
        sa = TenantStore(ta)
        # 确认启用了 WAL
        mode = sa._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

        errors: list = []

        def worker(n: int):
            try:
                docs = [SourceDoc(business_line="blx", title=f"t{n}-{i}",
                                  content=f"c{n}-{i}", source_type="text")
                        for i in range(5)]
                sa.save_documents(docs)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], msg=f"并发写入出现错误: {errors}")
        self.assertEqual(sa.stats("blx")["documents"], 30,
                         msg="并发写入后文档数量不符")

    def test_oauth_identity_mapping(self):
        dev = DevProvider()
        ext = dev.exchange("code-123")
        self.assertIsNone(self.cs.find_tenant_by_external("dev", ext.external_id))

        tid = self.cs.create_tenant(ext.name)
        self.cs.link_external("dev", ext.external_id, tid, "u1", ext.email)
        found = self.cs.find_tenant_by_external("dev", ext.external_id)
        self.assertEqual(found["tenant_id"], tid)
        # 同外部身份再次查找应保持映射稳定（幂等）
        self.assertEqual(self.cs.find_tenant_by_external("dev", ext.external_id)["tenant_id"], tid)

        # 企业微信 test_mode（无凭据也能验证映射逻辑）
        wecom = WeComProvider(test_mode=True)
        wext = wecom.exchange("wc-code")
        self.assertEqual(wext.provider, "wecom")
        self.assertTrue(wext.external_id.startswith("test-"))
        tid2 = self.cs.create_tenant(wext.name)
        self.cs.link_external("wecom", wext.external_id, tid2, "u2", wext.email)
        self.assertEqual(self.cs.find_tenant_by_external("wecom", wext.external_id)["tenant_id"], tid2)

    def test_jwt_and_password(self):
        tok = sign_jwt({"tid": "t1", "sub": "u1"}, secret="test-secret")
        body = verify_jwt(tok, "test-secret")
        self.assertEqual(body["tid"], "t1")
        self.assertEqual(body["sub"], "u1")

        # 错误密钥应失败
        with self.assertRaises(ValueError):
            verify_jwt(tok, "wrong-secret")

        h = hash_password("Str0ng!Pass")
        self.assertTrue(verify_password("Str0ng!Pass", h))
        self.assertFalse(verify_password("wrong-pass", h))
        self.assertFalse(verify_password("", h))


def main():
    try:
        unittest.main(verbosity=2, exit=False)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
