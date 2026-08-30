"""全局控制库（control plane）。

只保存「控制信息」：**租户登记**与**外部身份 → 租户映射**。
租户的私有业务数据一律存放在各自的 `tenants/<tid>/geo.db`，本库不接触。
即便控制库泄露，也不会直接暴露任何业务内容，降低横向移动风险。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from typing import Optional

from . import CONTROL_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_at TEXT,
    status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS id_map (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    user_id TEXT,
    email TEXT,
    created_at TEXT,
    PRIMARY KEY (provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_idmap_tenant ON id_map(tenant_id);
CREATE TABLE IF NOT EXISTS user_index (
    email TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL
);
"""


class ControlStore:
    """控制库封装（线程安全）。"""

    def __init__(self, db_path: str = CONTROL_DB) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 租户 ----
    def create_tenant(self, name: str) -> str:
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?,?,"
                "datetime('now'))",
                (tid, name),
            )
            self._conn.commit()
        return tid

    def get_tenant(self, tid: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM tenants WHERE id=?", (tid,))
            row = cur.fetchone()
            return dict(row) if row else None

    # ---- 外部身份映射 ----
    def find_tenant_by_external(self, provider: str, external_id: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM id_map WHERE provider=? AND external_id=?",
                (provider, external_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def link_external(self, provider: str, external_id: str, tenant_id: str,
                      user_id: Optional[str], email: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO id_map "
                "(provider, external_id, tenant_id, user_id, email, created_at) "
                "VALUES (?,?,?,?,?,datetime('now'))",
                (provider, external_id, tenant_id, user_id, email),
            )
            self._conn.commit()

    def list_tenants(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM tenants").fetchall()]

    # ---- 用户全局索引（仅存 email → 租户/用户，不含口令，便于按邮箱登录定位租户）----
    def index_user(self, email: str, tenant_id: str, user_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO user_index (email, tenant_id, user_id) VALUES (?,?,?)",
                (email, tenant_id, user_id),
            )
            self._conn.commit()

    def find_user_by_email(self, email: str) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM user_index WHERE email=?", (email,))
            row = cur.fetchone()
            return dict(row) if row else None


# 进程内单例
_cs: Optional["ControlStore"] = None
_cs_lock = threading.Lock()


def control_store() -> "ControlStore":
    global _cs
    with _cs_lock:
        if _cs is None:
            _cs = ControlStore()
        return _cs
