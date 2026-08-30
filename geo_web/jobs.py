"""后台作业管理器。

把可能长耗时的 GeoPipeline.run() 投递到线程池异步执行，立即返回 job_id；
作业状态/进度/结果持久化到租户库（jobs 表），任意 worker 都能轮询到，
满足多进程部署下的并发访问控制与「任务跑到哪了」的可观测性。
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from . import JOB_WORKERS
from .tenant import TenantContext, validate_id
from geo_engine.models import utcnow


class JobManager:
    def __init__(self, max_workers: int = JOB_WORKERS) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, tenant: TenantContext, bl_id: str,
               stages: Optional[List[str]] = None, force: bool = False,
               use_llm: bool = True) -> str:
        bl_id = validate_id(bl_id, "business_line")
        job_id = uuid.uuid4().hex
        created = utcnow()
        tenant.store().create_job(job_id, bl_id, ",".join(stages or []), created)
        self._pool.submit(self._run, tenant, job_id, bl_id, stages, force, use_llm)
        return job_id

    def _run(self, tenant: TenantContext, job_id: str, bl_id: str,
             stages: Optional[List[str]], force: bool, use_llm: bool) -> None:
        store = tenant.store()
        store.update_job(job_id, "running", progress="启动流水线")
        try:
            res = tenant.run_pipeline(bl_id, stages=stages, force=force, use_llm=use_llm)
            # 把产物落盘到租户 dist（核心引擎只返回内存 Artifact，由外壳层负责持久化）
            if res.artifacts:
                self._persist_artifacts(tenant, bl_id, res.artifacts)
            if res.ok():
                store.update_job(job_id, "succeeded",
                                 progress="完成", result=res.to_dict())
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

    def status(self, tenant: TenantContext, job_id: str) -> Optional[dict]:
        return tenant.store().get_job(job_id)


# 进程内单例
_manager: Optional[JobManager] = None


def job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
