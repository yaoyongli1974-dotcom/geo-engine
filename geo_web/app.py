"""FastAPI 应用 —— GEO 多用户服务器端 REST 接口。

设计要点：
  - 所有业务路由经 ``Depends(get_current_tenant)`` 强制注入租户上下文，
    存储/配置/产物一律带 tenant 维度，从框架层杜绝越权。
  - 长任务（GeoPipeline.run）一律投递后台作业，立即返回 job_id，避免阻塞 HTTP。
  - 认证支持：本地注册/登录（JWT + PBKDF2）+ 可插拔 OAuth（企业微信/Dev）。
  - 本文件不改动 geo_engine 任何核心代码，仅作为外壳层复用核心引擎。
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import (
    ACCESS_TOKEN_MIN,
    BASE_URL,
    CORS_ORIGINS,
    REFRESH_TOKEN_DAYS,
    ensure_base_dirs,
)
from .auth_providers import ExternalIdentity, get_provider
from .control import control_store
from .deps import get_current_tenant
from .jobs import job_manager
from .schemas import (
    ArtifactOut,
    BusinessLineIn,
    BusinessLineOut,
    ContentIn,
    JobOut,
    LoginRequest,
    MessageResponse,
    OAuthStartResponse,
    RefreshRequest,
    RegisterRequest,
    RunRequest,
    TokenResponse,
)
from .security import (
    gen_token_id,
    hash_password,
    refresh_expiry,
    sign_jwt,
    verify_jwt,
    verify_password,
)
from .tenant import TenantContext, TenantStore, validate_id
from geo_engine.config import dump_config
from geo_engine.models import slugify, utcnow

ensure_base_dirs()

app = FastAPI(title="GEO Web API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------- 认证辅助
def _new_refresh(store, user_id: str, tenant_id: str) -> str:
    """生成 refresh 令牌（格式 id.secret），落库哈希与过期，返回完整令牌。"""
    rid = gen_token_id()
    secret = gen_token_id() + gen_token_id()
    full = f"{rid}.{secret}"
    import hashlib
    h = hashlib.sha256(full.encode()).hexdigest()
    store.add_refresh_token(rid, user_id, tenant_id, h, refresh_expiry())
    return full


def _tokens(store, tenant_id: str, user_id: str) -> TokenResponse:
    access = sign_jwt({"tid": tenant_id, "sub": user_id})
    refresh = _new_refresh(store, user_id, tenant_id)
    return TokenResponse(access_token=access, refresh_token=refresh,
                         expires_in=ACCESS_TOKEN_MIN * 60)


def _oauth_provision(provider: str, ext: ExternalIdentity) -> tuple:
    """按外部身份查找/自动创建租户与用户，返回 (tenant_id, user_id)。"""
    cs = control_store()
    existing = cs.find_tenant_by_external(provider, ext.external_id)
    if existing:
        tid = existing["tenant_id"]
        store = TenantStore(tid)
        user = store.get_user_by_email(ext.email) if ext.email else None
        if user:
            return tid, user["id"]
        # 外部身份已绑定租户但无对应用户 → 建一个 member
        uid = "u_" + uuid.uuid4().hex[:12]
        store.upsert_user({"id": uid, "tenant_id": tid, "email": ext.email,
                           "name": ext.name, "role": "member",
                           "provider": provider, "external_id": ext.external_id,
                           "created_at": utcnow()})
        if ext.email:
            cs.index_user(ext.email, tid, uid)
        return tid, uid
    # 全新租户
    tid = cs.create_tenant(ext.name or ext.external_id)
    store = TenantStore(tid)
    uid = "u_" + uuid.uuid4().hex[:12]
    store.upsert_user({"id": uid, "tenant_id": tid, "email": ext.email,
                       "name": ext.name, "role": "owner",
                       "provider": provider, "external_id": ext.external_id,
                       "created_at": utcnow()})
    if ext.email:
        cs.index_user(ext.email, tid, uid)
    cs.link_external(provider, ext.external_id, tid, uid, ext.email)
    return tid, uid


# ---------------------------------------------------------------- /health
@app.get("/api/health")
def health():
    try:
        n = len(control_store().list_tenants())
    except Exception:
        n = -1
    return {"status": "ok", "service": "geo-web", "tenants": n, "time": utcnow()}


# ---------------------------------------------------------------- 认证：本地
@app.post("/api/auth/register", response_model=TokenResponse, status_code=201)
def register(body: RegisterRequest):
    cs = control_store()
    if cs.find_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="该邮箱已注册")
    tid = cs.create_tenant(body.org_name)
    store = TenantStore(tid)
    uid = "u_" + uuid.uuid4().hex[:12]
    store.upsert_user({
        "id": uid, "tenant_id": tid, "email": body.email, "name": body.name,
        "password_hash": hash_password(body.password), "role": "owner",
        "provider": "local", "created_at": utcnow(),
    })
    cs.index_user(body.email, tid, uid)
    return _tokens(store, tid, uid)


@app.post("/api/auth/login", response_model=TokenResponse)
def login(body: LoginRequest):
    cs = control_store()
    idx = cs.find_user_by_email(body.email)
    if not idx:
        raise HTTPException(status_code=401, detail="邮箱或口令错误")
    store = TenantStore(idx["tenant_id"])
    user = store.get_user_by_email(body.email)
    if not user or not verify_password(body.password, user.get("password_hash")):
        raise HTTPException(status_code=401, detail="邮箱或口令错误")
    return _tokens(store, idx["tenant_id"], user["id"])


def _ct_eq(a: str, b: str) -> bool:
    """恒定时间字符串比较（校验刷新令牌哈希）。"""
    import hmac as _hmac
    return _hmac.compare_digest(a, b)


def _locate_refresh_tenant(rid: str) -> Optional[str]:
    """在控制库记录的所有租户中查找持有该 refresh 的租户（MVP 简化；
    生产环境建议把 refresh 索引进控制库或 Redis）。"""
    cs = control_store()
    for t in cs.list_tenants():
        st = TenantStore(t["id"])
        row = st.get_refresh_token(rid)
        if row and not row.get("revoked"):
            return t["id"]
    return None


@app.post("/api/auth/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest):
    try:
        rid, _ = body.refresh_token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="刷新令牌格式错误")
    tid = _locate_refresh_tenant(rid)
    if not tid:
        raise HTTPException(status_code=401, detail="刷新令牌无效")
    store = TenantStore(tid)
    row = store.get_refresh_token(rid)
    if not row or row.get("revoked"):
        raise HTTPException(status_code=401, detail="刷新令牌已吊销")
    if row.get("expires_at") and float(row["expires_at"]) < time.time():
        raise HTTPException(status_code=401, detail="刷新令牌已过期")
    import hashlib
    if not _ct_eq(hashlib.sha256(body.refresh_token.encode()).hexdigest(), row["token_hash"]):
        raise HTTPException(status_code=401, detail="刷新令牌校验失败")
    store.revoke_refresh_token(rid)
    return _tokens(store, tid, row["user_id"])


@app.post("/api/auth/logout", response_model=MessageResponse)
def logout(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    if not creds or not creds.credentials:
        return MessageResponse(ok=True, message="无需登出")
    try:
        payload = verify_jwt(creds.credentials)
    except ValueError:
        return MessageResponse(ok=True, message="令牌已失效")
    # 从 refresh token 不可得，这里仅使当前 access 失去意义（无状态 JWT 无法主动吊销）
    # 生产建议：维护吊销列表或改用 refresh 轮转 + 短过期。
    return MessageResponse(ok=True, message="已登出（无状态令牌将在过期后失效）")


@app.get("/api/me")
def me(tenant: TenantContext = Depends(get_current_tenant),
       creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    uid = ""
    if creds and creds.credentials:
        try:
            uid = verify_jwt(creds.credentials).get("sub", "")
        except ValueError:
            uid = ""
    user = tenant.store().get_user(uid) if uid else None
    return {
        "tenant_id": tenant.tenant_id,
        "user": {"id": uid, "email": user.get("email") if user else None,
                 "name": user.get("name") if user else None,
                 "role": user.get("role") if user else None} if user else None,
    }


# ---------------------------------------------------------------- OAuth
@app.get("/api/auth/oauth/{provider}/start", response_model=OAuthStartResponse)
def oauth_start(provider: str, redirect_uri: Optional[str] = Query(None)):
    prov = get_provider(provider, _oauth_cfg(provider))
    state = gen_token_id()
    url = prov.authorize_url(state, redirect_uri)
    return OAuthStartResponse(provider=provider, authorize_url=url, state=state)


@app.get("/api/auth/oauth/{provider}/callback", response_model=TokenResponse)
def oauth_callback(provider: str, code: str = Query(...), state: str = Query("")):
    if not code:
        raise HTTPException(status_code=400, detail="缺少 code")
    prov = get_provider(provider, _oauth_cfg(provider))
    ext = prov.exchange(code)
    tid, uid = _oauth_provision(provider, ext)
    return _tokens(TenantStore(tid), tid, uid)


def _oauth_cfg(provider: str) -> Dict[str, str]:
    """从环境变量读取该提供方配置（无配置时提供方回落 test_mode）。"""
    p = provider.lower()
    return {
        "corpid": os.environ.get(f"GEO_{p.upper()}_CORPID", ""),
        "corpsecret": os.environ.get(f"GEO_{p.upper()}_CORPSECRET", ""),
        "agentid": os.environ.get(f"GEO_{p.upper()}_AGENTID", ""),
    }


# ---------------------------------------------------------------- 业务线
@app.get("/api/business-lines", response_model=List[BusinessLineOut])
def list_business_lines(tenant: TenantContext = Depends(get_current_tenant)):
    out = []
    for bl in tenant.repo.load_all():
        out.append(BusinessLineOut(
            id=bl.id, name=bl.name, description=bl.description,
            domain=bl.domain,
            sources=len(bl.sources),
            has_content=os.path.isdir(tenant.content_dir(bl.id)) and bool(
                os.listdir(tenant.content_dir(bl.id))),
        ))
    return out


@app.post("/api/business-lines", response_model=BusinessLineOut, status_code=201)
def create_business_line(body: BusinessLineIn,
                         tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(body.id, "business_line")
    try:
        tenant.repo.load(bl_id)
        raise HTTPException(status_code=409, detail=f"业务线已存在: {bl_id}")
    except FileNotFoundError:
        pass
    raw = body.model_dump(exclude_none=True)
    raw["id"] = bl_id
    # 自动挂载内容源（相对路径由 pipeline 的 root 解析到租户目录）
    if not raw.get("sources"):
        raw["sources"] = [{"type": "markdown_dir", "path": f"content/{bl_id}"}]
    tenant.settings.ensure_dirs(bl_id)
    # 以「写 JSON → 重新加载」确保业务线对象类型正确（嵌套 dataclass 由 ConfigRepository 负责）
    dump_config(raw, os.path.join(tenant.repo.settings.bl_dir(), f"{bl_id}.json"))
    obj = tenant.repo.load(bl_id)
    return BusinessLineOut(id=obj.id, name=obj.name, description=obj.description,
                           domain=obj.domain, sources=len(obj.sources),
                           has_content=False)


@app.get("/api/business-lines/{bl}")
def get_business_line(bl: str, tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    try:
        obj = tenant.repo.load(bl_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="业务线不存在")
    return obj.to_dict()


@app.put("/api/business-lines/{bl}/content", response_model=MessageResponse)
def put_content(bl: str, body: ContentIn,
                tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    try:
        tenant.repo.load(bl_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="业务线不存在")
    cdir = tenant.content_dir(bl_id)
    os.makedirs(cdir, exist_ok=True)
    fname = slugify(body.title) or "doc"
    # 避免重名覆盖：追加短随机
    path = os.path.join(cdir, f"{fname}.md")
    if os.path.exists(path):
        path = os.path.join(cdir, f"{fname}-{uuid.uuid4().hex[:6]}.md")
    meta = f"---\ntitle: {body.title}\nauthority: {body.authority}\n---\n\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(meta + body.content)
    return MessageResponse(ok=True, message=f"已写入 {os.path.relpath(path, tenant.root)}")


# ---------------------------------------------------------------- 运行 / 作业
@app.post("/api/business-lines/{bl}/run", response_model=JobOut, status_code=202)
def run_business_line(bl: str, body: RunRequest,
                     tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    try:
        tenant.repo.load(bl_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="业务线不存在")
    job_id = job_manager().submit(tenant, bl_id, stages=body.stages,
                                  force=body.force, use_llm=body.use_llm)
    return JobOut(id=job_id, business_line=bl_id, status="queued")


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def job_status(job_id: str, tenant: TenantContext = Depends(get_current_tenant)):
    row = job_manager().status(tenant, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="作业不存在")
    # 作业属于该租户（job 表中 business_line 属租户库，越权读不到）
    return JobOut(**{k: row.get(k) for k in JobOut.model_fields})


# ---------------------------------------------------------------- 产物 / 报表
@app.get("/api/business-lines/{bl}/artifacts", response_model=List[ArtifactOut])
def list_artifacts(bl: str, tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    ddir = tenant.dist_dir(bl_id)
    out: List[ArtifactOut] = []
    if not os.path.isdir(ddir):
        return out
    for root, _, files in os.walk(ddir):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, ddir).replace("\\", "/")
            try:
                cs = _file_sha1(full)
            except OSError:
                cs = ""
            out.append(ArtifactOut(path=rel, format=os.path.splitext(fn)[1].lstrip("."),
                                   checksum=cs))
    return out


@app.get("/api/artifacts/{bl}/{path:path}")
def serve_artifact(bl: str, path: str,
                   tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    # 路径穿越防护：归一化后必须仍落在租户产物目录内
    full = os.path.normpath(os.path.join(tenant.dist_dir(bl_id), path))
    base = os.path.normpath(tenant.dist_dir(bl_id))
    if not full.startswith(base + os.sep) and full != base:
        raise HTTPException(status_code=400, detail="非法路径")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="产物不存在")
    return FileResponse(full)


@app.get("/api/business-lines/{bl}/report")
def get_report(bl: str, tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    rdir = tenant.report_dir(bl_id)
    if not os.path.isdir(rdir):
        raise HTTPException(status_code=404, detail="暂无报表")
    files = sorted(os.listdir(rdir), reverse=True)
    if not files:
        raise HTTPException(status_code=404, detail="暂无报表")
    latest = files[0]
    full = os.path.join(rdir, latest)
    if latest.endswith(".json"):
        import json
        with open(full, "r", encoding="utf-8") as f:
            return JSONResponse({"file": latest, "data": json.load(f)})
    with open(full, "r", encoding="utf-8", errors="ignore") as f:
        return {"file": latest, "text": f.read()[:20000]}


# ---------------------------------------------------------------- 内部工具
def _file_sha1(path: str) -> str:
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
