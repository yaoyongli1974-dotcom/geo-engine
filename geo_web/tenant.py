"""租户上下文（tenant namespace）。

核心职责：
  1. 规范化并校验 tenant_id / bl_id（路径穿越防护）；
  2. 为某个租户提供隔离的 Store（每租户一个 SQLite 文件 + WAL）；
  3. 构造「租户作用域」的 Settings 与 ConfigRepository —— 由于核心引擎的
     ConfigRepository 只认 settings.bl_dir()，只要把 Settings 的 root 指向
     tenants/<tid>，业务线配置/内容/产物**天然**按租户隔离，无需改核心代码；
  4. 提供 run_pipeline() 便捷方法，复用 GeoPipeline 原样。
"""

from __future__ import annotations

import os
import re
import threading
from typing import List, Optional

from geo_engine.config import ConfigRepository, Settings
from geo_engine.models import slugify
from geo_engine.pipeline import GeoPipeline
from geo_engine.store import Store
from . import TENANTS_DIR

#: 允许的 tenant_id / bl_id 字符（字母数字 + 下划线 + 连字符），拒绝 .. / 绝对路径
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$")


def validate_id(value: str, what: str = "id") -> str:
    """校验租户/业务线标识，非法（含路径穿越）一律拒绝。"""
    if not value or not _SAFE_ID.match(value):
        raise ValueError(
            f"非法的{what}：只允许字母数字/下划线/连字符，长度 1-60，"
            f"且不得包含 .. / 或绝对路径（收到：{value!r}）"
        )
    return value


class TenantContext:
    """单个租户的运行上下文（隔离边界）。"""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = validate_id(tenant_id, "tenant_id")
        self.root = os.path.join(TENANTS_DIR, self.tenant_id)
        os.makedirs(self.root, exist_ok=True)
        # 租户私有布局：db 直接落在 root，业务线/内容/产物/报表为子目录
        self.settings = Settings(self.root, {
            "layout": {
                "business_lines": "business_lines",
                "content": "content",
                "dist": "dist",
                "data": ".",            # geo.db 直接放 root
                "reports": "reports",
            }
        })
        self.repo = ConfigRepository(self.settings)
        self.settings.ensure_dirs()

    # ---- 存储（进程内按 tid 缓存，跨 worker 由 WAL 兜底）----
    def store(self) -> Store:
        return Store.for_tenant(self.tenant_id)

    # ---- 业务线目录 / 产物目录（带校验）----
    def content_dir(self, bl_id: str) -> str:
        return self.settings.content_dir(validate_id(bl_id, "business_line"))

    def dist_dir(self, bl_id: str) -> str:
        return self.settings.dist_dir(validate_id(bl_id, "business_line"))

    def report_dir(self, bl_id: str) -> str:
        return self.settings.report_dir(validate_id(bl_id, "business_line"))

    # ---- 运行核心引擎（原样复用，零改动）----
    def run_pipeline(self, bl_id: str, stages: Optional[List[str]] = None,
                     force: bool = False, use_llm: bool = True):
        bl_id = validate_id(bl_id, "business_line")
        pipeline = GeoPipeline(self.settings, self.store())
        return pipeline.run(bl_id, stages=stages, force=force, use_llm=use_llm)


class _TenantManager:
    """进程内 Store 缓存（按 tenant_id）。多进程部署时各 worker 各自缓存，
    落盘数据以 WAL SQLite 为准，安全。"""

    def __init__(self) -> None:
        self._cache: dict = {}
        self._lock = threading.RLock()

    def get(self, tenant_id: str) -> Store:
        tenant_id = validate_id(tenant_id, "tenant_id")
        with self._lock:
            st = self._cache.get(tenant_id)
            if st is None:
                db_path = os.path.join(TENANTS_DIR, tenant_id, "geo.db")
                st = Store(db_path, wal=True)
                self._cache[tenant_id] = st
            return st

    def drop(self, tenant_id: str) -> None:
        with self._lock:
            self._cache.pop(tenant_id, None)


# 模块级单例（进程内）
_MANAGER = _TenantManager()


def TenantStore(tenant_id: str) -> Store:
    """便捷获取某租户的 Store（每租户独立库 + WAL）。"""
    return _MANAGER.get(tenant_id)


def for_tenant(tenant_id: str) -> TenantContext:
    """构造租户上下文。"""
    return TenantContext(tenant_id)


# 给 Store 增加类方法 for_tenant，便于 `Store.for_tenant(tid)` 风格调用
def _store_for_tenant(cls, tenant_id: str) -> Store:  # type: ignore[unused]
    return _MANAGER.get(tenant_id)


Store.for_tenant = classmethod(_store_for_tenant)  # type: ignore[attr-defined]
