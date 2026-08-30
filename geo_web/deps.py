"""FastAPI 依赖：从 Bearer JWT 解析出租户上下文，强制数据隔离。

所有受保护路由都通过 `Depends(get_current_tenant)` 拿到 TenantContext，
内部一切存储/配置/产物查询默认带 tenant 维度——从框架层消除越权可能。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import JWT_SECRET
from .control import control_store
from .security import verify_jwt
from .tenant import TenantContext, validate_id

_bearer = HTTPBearer(auto_error=True)

# 校验失败统一 401
_AUTH_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="未认证或令牌无效",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_tenant(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> TenantContext:
    if not creds or not creds.credentials:
        raise _AUTH_ERROR
    try:
        payload = verify_jwt(creds.credentials, JWT_SECRET)
    except ValueError as exc:
        raise _AUTH_ERROR from exc
    tid = payload.get("tid")
    if not tid:
        raise _AUTH_ERROR
    # 再次校验，防止令牌内 tid 被篡改成非法路径
    try:
        validate_id(tid, "tenant_id")
    except ValueError as exc:
        raise _AUTH_ERROR from exc
    if not control_store().get_tenant(tid):
        # 控制库无此租户（极端情况：令牌与控制库不一致）→ 拒绝
        raise _AUTH_ERROR
    return TenantContext(tid)


def current_user_id(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """返回当前用户 id（部分写操作需审计）。"""
    try:
        payload = verify_jwt(creds.credentials, JWT_SECRET)
    except ValueError as exc:
        raise _AUTH_ERROR from exc
    return payload.get("sub", "")
