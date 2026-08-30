"""AI 辅助任务后台管理器。

把「内容完善 / 内容生成 / 联网搜索整合」三类可能较慢的 AI 调用投递到线程池，
立即返回 job_id；状态/结果写入租户库 jobs 表，前端可沿用现有轮询机制读取结果。

与 pipeline 的 JobManager 分离，避免相互阻塞；共用 jobs 表结构。
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from . import JOB_WORKERS
from .ai import ai_complete, ai_generate, ai_research, load_settings
from .tenant import TenantContext, validate_id
from geo_engine.models import utcnow


def _bl_context(tenant: TenantContext, bl_id: Optional[str]) -> str:
    """构造业务线背景文本，用于增强提示词。"""
    if not bl_id:
        return ""
    try:
        bl = tenant.repo.load(validate_id(bl_id, "business_line"))
    except FileNotFoundError:
        return ""
    parts = [f"业务线：{bl.name}"]
    if bl.description:
        parts.append(f"简介：{bl.description}")
    if bl.domain:
        parts.append(f"官网：{bl.domain}")
    if getattr(bl, "topics", None):
        parts.append("主题：" + "、".join(bl.topics))
    return "\n".join(parts)


class AIJobManager:
    def __init__(self, max_workers: int = max(1, JOB_WORKERS)) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, tenant: TenantContext, kind: str, payload: Dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        created = utcnow()
        tenant.store().create_job(job_id, payload.get("business_line") or "ai", kind, created)
        tenant.store().update_job(job_id, "running", progress="准备调用 AI")
        self._pool.submit(self._run, tenant, job_id, kind, payload)
        return job_id

    def _run(self, tenant: TenantContext, job_id: str, kind: str, payload: Dict[str, Any]) -> None:
        store = tenant.store()
        try:
            settings = load_settings(store)
            if not settings or not (settings.get("api_key") or settings.get("provider") == "ollama"):
                store.update_job(job_id, "failed",
                                 error="尚未接入大模型：请先在「AI 配置」中填写 API Key 并保存")
                return
            ctx = _bl_context(tenant, payload.get("business_line"))
            if kind == "ai_complete":
                text = ai_complete(settings, payload.get("text", ""), payload.get("instruction", ""), ctx)
                store.update_job(job_id, "succeeded", progress="已完善内容",
                                 result={"text": text, "sources": []})
            elif kind == "ai_generate":
                text = ai_generate(settings, payload.get("topic", ""), ctx,
                                   payload.get("tone", "专业严谨"), payload.get("length", "中等"))
                store.update_job(job_id, "succeeded", progress="已生成草稿",
                                 result={"text": text, "sources": []})
            elif kind == "ai_research":
                res = ai_research(settings, payload.get("query", ""), ctx)
                store.update_job(job_id, "succeeded", progress="已完成联网调研",
                                 result={"text": res.get("text", ""), "sources": res.get("sources", [])})
            else:
                store.update_job(job_id, "failed", error=f"未知任务类型：{kind}")
        except Exception as exc:  # AIError 或网络异常统一落为失败
            store.update_job(job_id, "failed", error=str(exc))

    def status(self, tenant: TenantContext, job_id: str) -> Optional[dict]:
        return tenant.store().get_job(job_id)


_manager: Optional[AIJobManager] = None


def ai_job_manager() -> AIJobManager:
    global _manager
    if _manager is None:
        _manager = AIJobManager()
    return _manager
