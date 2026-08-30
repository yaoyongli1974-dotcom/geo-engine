"""后台作业管理器。

把可能长耗时的 GeoPipeline.run() 投递到线程池异步执行，立即返回 job_id；
作业状态/进度/结果持久化到租户库（jobs 表），任意 worker 都能轮询到，
满足多进程部署下的并发访问控制与「任务跑到哪了」的可观测性。
"""

from __future__ import annotations

import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from . import BASE_URL, JOB_WORKERS, PUBLISHED_DIR
from .tenant import TenantContext, validate_id
from geo_engine.models import utcnow


class JobManager:
    def __init__(self, max_workers: int = JOB_WORKERS) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, tenant: TenantContext, bl_id: str,
               stages: Optional[List[str]] = None, force: bool = False,
               use_llm: bool = True, publish: bool = False) -> str:
        bl_id = validate_id(bl_id, "business_line")
        job_id = uuid.uuid4().hex
        created = utcnow()
        tenant.store().create_job(job_id, bl_id, ",".join(stages or []), created)
        self._pool.submit(self._run, tenant, job_id, bl_id, stages, force, use_llm, publish)
        return job_id

    def _run(self, tenant: TenantContext, job_id: str, bl_id: str,
             stages: Optional[List[str]], force: bool, use_llm: bool,
             publish: bool = False) -> None:
        store = tenant.store()
        store.update_job(job_id, "running", progress="启动流水线")
        try:
            res = tenant.run_pipeline(bl_id, stages=stages, force=force, use_llm=use_llm)
            # 把产物落盘到租户 dist（核心引擎只返回内存 Artifact，由外壳层负责持久化）
            if res.artifacts:
                self._persist_artifacts(tenant, bl_id, res.artifacts)
            if res.ok():
                progress = "完成"
                if publish:
                    try:
                        urls = self._publish_artifacts(tenant, bl_id, job_id)
                        progress = f"已发布 {len(urls)} 个产物"
                    except Exception as exc:  # 发布失败不应让整任务判失败
                        progress = f"生成完成，但发布失败：{exc}"
                store.update_job(job_id, "succeeded",
                                 progress=progress, result=res.to_dict())
            else:
                store.update_job(job_id, "failed",
                                 progress="存在错误", error="; ".join(res.errors),
                                 result=res.to_dict())
        except Exception as exc:  # 兜底，避免线程静默退出
            store.update_job(job_id, "failed", error=str(exc))

    @staticmethod
    def _persist_artifacts(tenant: TenantContext, bl_id: str,
                           artifacts: List) -> None:
        """将内存产物写入租户隔离的 dist 目录，并登记 artifacts 表。"""
        import hashlib

        ddir = tenant.dist_dir(bl_id)
        os.makedirs(ddir, exist_ok=True)
        items: List[Dict[str, object]] = []
        for a in artifacts:
            content = getattr(a, "content", "") or ""
            if not content:
                continue
            full = os.path.join(ddir, a.path)
            parent = os.path.dirname(full)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            items.append({
                "path": a.path,
                "format": getattr(a, "format", "text"),
                "checksum": hashlib.sha1(content.encode("utf-8")).hexdigest(),
                "updated_at": getattr(a, "updated_at", utcnow()),
            })
        if items:
            tenant.store().mark_artifacts(bl_id, items)

    @staticmethod
    def _publish_artifacts(tenant: TenantContext, bl_id: str, job_id: str) -> List[str]:
        """把 dist 产物复制到全局公开目录，记录发布历史，返回公开 URL 列表。

        公开目录 PUBLISHED_DIR/<bl>/ 由 GET /p/{bl}/{path} 直接对外提供（无需鉴权），
        供 ChatGPT / Perplexity 等生成式引擎抓取——这正是 GEO 的核心价值。
        """
        src = tenant.dist_dir(bl_id)
        if not os.path.isdir(src):
            return []
        os.makedirs(PUBLISHED_DIR, exist_ok=True)
        dst = os.path.join(PUBLISHED_DIR, bl_id)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        # 收集公开 URL（基于 BASE_URL，确保域名正确）
        urls: List[str] = []
        for root, _, files in os.walk(dst):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, dst).replace("\\", "/")
                urls.append(f"{BASE_URL}/p/{bl_id}/{rel}")
        # 记录发布历史
        tenant.store().add_publish(uuid.uuid4().hex, bl_id, urls, job_id, utcnow())
        return urls

    def status(self, tenant: TenantContext, job_id: str) -> Optional[dict]:
        return tenant.store().get_job(job_id)


# 进程内单例
_manager: Optional[JobManager] = None


def job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
