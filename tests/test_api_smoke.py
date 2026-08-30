"""GEO Web API 端到端冒烟测试（需 fastapi/uvicorn/pydantic/httpx）。

覆盖：
  - /api/health
  - 本地注册 → 拿到 JWT → /api/me
  - 创建业务线 → 上传内容 → 触发后台作业 → 轮询作业直到成功
  - 列出产物（应包含 llms.txt 等）
  - 企业微信/Dev OAuth：start → callback(code) → 拿到令牌
  - 跨租户隔离：租户B 看不到租户A 的业务线

运行（在装好依赖的 venv 中）：
    python -m tests.test_api_smoke
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="geo_api_test_")
os.environ["GEO_DATA_DIR"] = _TMP
os.environ["GEO_JWT_SECRET"] = "test-secret-only"
os.environ["GEO_CORS_ORIGINS"] = "*"

from fastapi.testclient import TestClient  # noqa: E402

from geo_web.app import app  # noqa: E402


class ApiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _register(self, org: str, email: str, pw: str = "Str0ng!Pass") -> dict:
        r = self.client.post("/api/auth/register", json={
            "org_name": org, "email": email, "name": org, "password": pw})
        self.assertEqual(r.status_code, 201, msg=r.text)
        return r.json()

    def _auth(self, tokens: dict) -> dict:
        return {"Authorization": f"Bearer {tokens['access_token']}"}

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_full_flow_local(self):
        tok = self._register("租户甲", "alice@acme.test")
        h = self._auth(tok)

        # /api/me
        r = self.client.get("/api/me", headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["email"], "alice@acme.test")

        # 创建业务线
        r = self.client.post("/api/business-lines", headers=h, json={
            "id": "demo", "name": "示例业务线", "description": "smoke"})
        self.assertEqual(r.status_code, 201, msg=r.text)
        self.assertEqual(r.json()["id"], "demo")

        # 上传内容
        r = self.client.put("/api/business-lines/demo/content", headers=h, json={
            "title": "关于我们的服务", "content": "# 关于我们\n我们提供弱电智能化集成服务，覆盖综合布线、楼宇自控与安防监控。",
            "authority": 3})
        self.assertEqual(r.status_code, 200, msg=r.text)

        # 触发运行（后台作业，离线启发式，不联网）
        r = self.client.post("/api/business-lines/demo/run", headers=h,
                             json={"use_llm": False, "force": True})
        self.assertEqual(r.status_code, 202, msg=r.text)
        job_id = r.json()["id"]

        # 轮询作业直到终态
        final = None
        for _ in range(100):
            r = self.client.get(f"/api/jobs/{job_id}", headers=h)
            self.assertEqual(r.status_code, 200)
            final = r.json()
            if final["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.2)
        self.assertEqual(final["status"], "succeeded",
                         msg=f"作业失败: {final.get('error')} | {final.get('result')}")

        # 列出产物（应包含 llms.txt）
        r = self.client.get("/api/business-lines/demo/artifacts", headers=h)
        self.assertEqual(r.status_code, 200)
        paths = [a["path"] for a in r.json()]
        self.assertTrue(any(p.endswith("llms.txt") for p in paths),
                        msg=f"未生成 llms.txt，产物: {paths}")

        # 报表可查
        r = self.client.get("/api/business-lines/demo/report", headers=h)
        self.assertIn(r.status_code, (200, 404))

    def test_oauth_dev_flow(self):
        # dev 提供方：start 返回授权地址
        r = self.client.get("/api/auth/oauth/dev/start")
        self.assertEqual(r.status_code, 200)
        self.assertIn("authorize_url", r.json())

        # callback(code) 自动开通租户并返回令牌
        r = self.client.get("/api/auth/oauth/dev/callback", params={
            "code": "dev-smoke", "state": "x"})
        self.assertEqual(r.status_code, 200, msg=r.text)
        tok = r.json()
        self.assertIn("access_token", tok)

        h = self._auth(tok)
        r = self.client.get("/api/me", headers=h)
        self.assertEqual(r.status_code, 200)

    def test_cross_tenant_isolation(self):
        a = self._register("租户A", "a@iso.test")
        b = self._register("租户B", "b@iso.test")

        # A 建业务线
        self.client.post("/api/business-lines", headers=self._auth(a),
                         json={"id": "bl_a", "name": "A的业务线"})

        # B 看不到 A 的业务线
        r = self.client.get("/api/business-lines", headers=self._auth(b))
        self.assertEqual(r.status_code, 200)
        ids = [x["id"] for x in r.json()]
        self.assertNotIn("bl_a", ids, msg=f"B 越权看到了 A 的业务线: {ids}")

        # B 也不能直接读取 A 的业务线配置（404）
        r = self.client.get("/api/business-lines/bl_a", headers=self._auth(b))
        self.assertEqual(r.status_code, 404)

        # 无令牌访问受保护接口应 403/401
        r = self.client.get("/api/business-lines")
        self.assertIn(r.status_code, (401, 403))


def main():
    try:
        unittest.main(verbosity=2, exit=False)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
