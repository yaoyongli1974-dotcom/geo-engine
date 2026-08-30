"""GEO Web —— 多用户服务器端外壳层。

本包在**不改动** `geo_engine` 核心算法的前提下，提供：
  - 租户（tenant）命名空间：数据/配置/产物按租户隔离
  - 身份认证与会话（JWT + 可插拔 OAuth / 企业微信）
  - 后台作业（长任务异步化，避免阻塞 HTTP）
  - FastAPI REST 接口

核心引擎 `geo_engine.ingest/structure/semantic/formats/distribute/monitor/pipeline`
保持原样复用。

数据布局（默认，可用环境变量 GEO_DATA_DIR 覆盖）：
    <GEO_DATA_DIR>/control.db            全局控制库（租户登记 + 外部身份映射）
    <GEO_DATA_DIR>/tenants/<tid>/geo.db  租户私有库（业务数据 + 用户 + 作业）
    <GEO_DATA_DIR>/tenants/<tid>/business_lines/*.json
    <GEO_DATA_DIR>/tenants/<tid>/content/<bl>/
    <GEO_DATA_DIR>/tenants/<tid>/dist/<bl>/
    <GEO_DATA_DIR>/tenants/<tid>/reports/<bl>/
"""

from __future__ import annotations

import os

# ---- 路径约定 ----
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_PKG_DIR)

#: 数据根目录；默认 <repo>/data，可用环境变量覆盖（部署时指向持久卷）
DATA_DIR = os.environ.get("GEO_DATA_DIR") or os.path.join(REPO_ROOT, "data")

CONTROL_DB = os.path.join(DATA_DIR, "control.db")
TENANTS_DIR = os.path.join(DATA_DIR, "tenants")
#: 公开发布目录：publish 后的产物复制到此处，由 GET /p/{bl}/{path} 对外提供（无需鉴权）
PUBLISHED_DIR = os.path.join(DATA_DIR, "published")

#: JWT / 会话配置（务必通过环境变量注入高熵密钥，绝不入库）
JWT_SECRET = os.environ.get("GEO_JWT_SECRET") or os.environ.get("JWT_SECRET") \
    or "INSECURE_DEV_SECRET_CHANGE_ME"
ACCESS_TOKEN_MIN = int(os.environ.get("GEO_ACCESS_MIN", "15"))
REFRESH_TOKEN_DAYS = int(os.environ.get("GEO_REFRESH_DAYS", "7"))

#: 后台作业并发上限（单进程）
JOB_WORKERS = int(os.environ.get("GEO_JOB_WORKERS", "2"))

#: CORS 允许来源（逗号分隔）
CORS_ORIGINS = [o.strip() for o in os.environ.get("GEO_CORS_ORIGINS", "*").split(",") if o.strip()]

#: 服务对外基础 URL（用于产物链接、OAuth 回调拼接）
BASE_URL = os.environ.get("GEO_BASE_URL", "http://localhost:8000").rstrip("/")


def ensure_base_dirs() -> None:
    os.makedirs(TENANTS_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
