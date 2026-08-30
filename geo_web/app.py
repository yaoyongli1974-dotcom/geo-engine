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
import json
import time
import uuid
import shutil
import mimetypes
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from . import (
    ACCESS_TOKEN_MIN,
    BASE_URL,
    CORS_ORIGINS,
    PUBLISHED_DIR,
    REFRESH_TOKEN_DAYS,
    ensure_base_dirs,
)
from .auth_providers import ExternalIdentity, get_provider
from .control import control_store
from .deps import get_current_tenant
from .jobs import job_manager
from .schemas import (
    ArtifactOut,
    AISettingsIn,
    AISettingsOut,
    AIValidateResponse,
    AICompleteRequest,
    AIGenerateRequest,
    AIResearchRequest,
    BusinessLineIn,
    BusinessLineOut,
    BusinessLineUpdate,
    ContentDelete,
    ContentGet,
    ContentIn,
    ContentMeta,
    ContentUpdate,
    JobOut,
    LoginRequest,
    MessageResponse,
    OAuthStartResponse,
    PublishOut,
    PublishRecord,
    PublishRequest,
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
from .crypto import encrypt_secret, decrypt_secret, redact
from .ai import load_settings, validate_key, preset_list, AIError
from .ai_jobs import ai_job_manager
from .tenant import TenantContext, TenantStore, validate_id
from geo_engine.config import dump_config
from geo_engine.models import slugify, utcnow

#: 前端静态资源目录（SPA：index.html + app.js + style.css）
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

ensure_base_dirs()

app = FastAPI(title="GEO Web API", version="1.0.0", redirect_slashes=False)
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


# ---------------------------------------------------------------- 内容管理辅助
def _safe_content_name(name: str) -> str:
    """把内容文件名规整为安全文件名（带 .md，禁止路径穿越）。"""
    name = os.path.basename(name.strip())
    if not name or ".." in name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="非法的文件名")
    if not name.endswith(".md"):
        name += ".md"
    return name


def _content_path(tenant: TenantContext, bl_id: str, name: str) -> str:
    bl_id = validate_id(bl_id, "business_line")
    return os.path.join(tenant.content_dir(bl_id), _safe_content_name(name))


def _parse_content_file(path: str) -> ContentGet:
    base = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return ContentGet(name=base, title="", authority=2, content="")
    title, authority, content = "", 2, raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            fm = raw[3:end].strip()
            body = raw[end + 4:].lstrip("\n")
            meta: Dict[str, str] = {}
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            title = meta.get("title", "")
            try:
                authority = int(meta.get("authority", "2") or 2)
            except ValueError:
                authority = 2
            content = body
    return ContentGet(name=base, title=title, authority=authority, content=content)


def _serialize_content(title: str, authority: int, content: str) -> str:
    return f"---\ntitle: {title}\nauthority: {authority}\n---\n\n{content}"


# ---------------------------------------------------------------- 内容：列表 / 获取 / 新建 / 更新 / 删除
@app.get("/api/business-lines/{bl}/contents", response_model=List[ContentMeta])
def list_contents(bl: str, tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    cdir = tenant.content_dir(bl_id)
    if not os.path.isdir(cdir):
        return []
    out: List[ContentMeta] = []
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".md"):
            continue
        full = os.path.join(cdir, fn)
        try:
            st = os.stat(full)
        except OSError:
            continue
        parsed = _parse_content_file(full)
        out.append(ContentMeta(
            name=parsed.name, title=parsed.title, authority=parsed.authority,
            size=st.st_size,
            updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        ))
    return out


@app.get("/api/business-lines/{bl}/contents/{name}", response_model=ContentGet)
def get_content(bl: str, name: str, tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    path = _content_path(tenant, bl_id, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="内容不存在")
    return _parse_content_file(path)


@app.post("/api/business-lines/{bl}/content", response_model=MessageResponse, status_code=201)
def create_content(bl: str, body: ContentIn,
                   tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    try:
        tenant.repo.load(bl_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="业务线不存在")
    cdir = tenant.content_dir(bl_id)
    os.makedirs(cdir, exist_ok=True)
    name = body.name or slugify(body.title) or "doc"
    path = _content_path(tenant, bl_id, name)
    if os.path.isfile(path):
        # 新建接口遇重名则追加短随机，避免静默覆盖
        path = _content_path(tenant, bl_id, f"{name}-{uuid.uuid4().hex[:6]}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_serialize_content(body.title, body.authority, body.content))
    return MessageResponse(ok=True, message=f"已创建内容 {os.path.basename(path)}")


@app.put("/api/business-lines/{bl}/content", response_model=MessageResponse)
def update_content(bl: str, body: ContentUpdate,
                   tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    path = _content_path(tenant, bl_id, body.name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="内容不存在")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_serialize_content(body.title, body.authority, body.content))
    return MessageResponse(ok=True, message=f"已更新内容 {body.name}")


@app.delete("/api/business-lines/{bl}/content", response_model=MessageResponse)
def delete_content(bl: str, body: ContentDelete,
                   tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    path = _content_path(tenant, bl_id, body.name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="内容不存在")
    os.remove(path)
    return MessageResponse(ok=True, message=f"已删除内容 {body.name}")


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


# ---------------------------------------------------------------- 业务线：更新 / 删除
@app.put("/api/business-lines/{bl}", response_model=BusinessLineOut)
def update_business_line(bl: str, body: BusinessLineUpdate,
                         tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    try:
        tenant.repo.load(bl_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="业务线不存在")
    bl_path = os.path.join(tenant.repo.settings.bl_dir(), f"{bl_id}.json")
    existing: Dict[str, Any] = {}
    if os.path.isfile(bl_path):
        try:
            with open(bl_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = {}
    raw = body.model_dump(exclude_none=True)
    raw["id"] = bl_id
    if not raw.get("sources") and existing.get("sources"):
        raw["sources"] = existing["sources"]
    if not raw.get("sources"):
        raw["sources"] = [{"type": "markdown_dir", "path": f"content/{bl_id}"}]
    dump_config(raw, bl_path)
    obj = tenant.repo.load(bl_id)
    cdir = tenant.content_dir(bl_id)
    return BusinessLineOut(id=obj.id, name=obj.name, description=obj.description,
                           domain=obj.domain, sources=len(obj.sources),
                           has_content=os.path.isdir(cdir) and bool(os.listdir(cdir)))


@app.delete("/api/business-lines/{bl}", response_model=MessageResponse)
def delete_business_line(bl: str, tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    try:
        tenant.repo.load(bl_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="业务线不存在")
    bl_dir = tenant.repo.settings.bl_dir()
    bl_file = os.path.join(bl_dir, f"{bl_id}.json")
    if os.path.isfile(bl_file):
        os.remove(bl_file)
    for d in (tenant.content_dir(bl_id), tenant.dist_dir(bl_id), tenant.report_dir(bl_id)):
        if os.path.isdir(d):
            shutil.rmtree(d)
    pub = os.path.join(PUBLISHED_DIR, bl_id)
    if os.path.isdir(pub):
        shutil.rmtree(pub)
    return MessageResponse(ok=True, message=f"已删除业务线 {bl_id}")


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


# ---------------------------------------------------------------- 发布（生成并对外公开）
@app.post("/api/business-lines/{bl}/publish", response_model=PublishOut, status_code=202)
def publish_business_line(bl: str, body: PublishRequest,
                          tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    try:
        tenant.repo.load(bl_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="业务线不存在")
    # 触发生成任务并标记发布：任务成功后由 jobs._publish_artifacts 复制到公开目录
    job_id = job_manager().submit(tenant, bl_id, stages=body.stages,
                                  force=body.force, use_llm=body.use_llm, publish=True)
    return PublishOut(job_id=job_id, business_line=bl_id, status="queued",
                      urls=_published_urls(bl_id))


@app.get("/api/business-lines/{bl}/publishes", response_model=List[PublishRecord])
def list_publishes(bl: str, tenant: TenantContext = Depends(get_current_tenant)):
    bl_id = validate_id(bl, "business_line")
    rows = tenant.store().get_publishes(bl_id)
    return [PublishRecord(id=r["id"], business_line=r["business_line"],
                          urls=r.get("urls") or [], published_at=r.get("published_at", ""),
                          job_id=r.get("job_id", "")) for r in rows]


def _published_urls(bl_id: str) -> List[str]:
    """列出该业务线公开目录已有产物对应的 URL（供前端预展示）。"""
    d = os.path.join(PUBLISHED_DIR, bl_id)
    urls: List[str] = []
    if not os.path.isdir(d):
        return urls
    for root, _, files in os.walk(d):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, d).replace("\\", "/")
            urls.append(f"{BASE_URL}/p/{bl_id}/{rel}")
    return sorted(urls)


# ---------------------------------------------------------------- 用户 AI 配置与能力
@app.get("/api/ai/settings", response_model=AISettingsOut)
def get_ai_settings(tenant: TenantContext = Depends(get_current_tenant)):
    row = tenant.store().get_ai_settings()
    if not row:
        return AISettingsOut(presets=preset_list())
    extra = {}
    if row.get("extra"):
        try:
            extra = json.loads(row["extra"])
        except (ValueError, TypeError):
            extra = {}
    return AISettingsOut(
        provider=row.get("provider") or "",
        base_url=row.get("base_url") or "",
        model=row.get("model") or "",
        has_key=bool(row.get("api_key_enc")),
        api_key_masked=redact(decrypt_secret(row.get("api_key_enc") or "")) if row.get("api_key_enc") else "",
        search_provider=row.get("search_provider") or "none",
        has_search_key=bool(row.get("search_key_enc")),
        search_key_masked=redact(decrypt_secret(row.get("search_key_enc") or "")) if row.get("search_key_enc") else "",
        validated=bool(row.get("validated")),
        validated_at=row.get("validated_at") or "",
        temperature=float(extra.get("temperature", 0.7)),
        note=extra.get("note", ""),
        presets=preset_list(),
    )


@app.put("/api/ai/settings", response_model=MessageResponse)
def put_ai_settings(body: AISettingsIn, tenant: TenantContext = Depends(get_current_tenant)):
    existing = tenant.store().get_ai_settings() or {}
    api_key_enc = existing.get("api_key_enc") or ""
    if body.api_key and body.api_key != "***":
        api_key_enc = encrypt_secret(body.api_key)
    search_key_enc = existing.get("search_key_enc") or ""
    if body.search_key and body.search_key != "***":
        search_key_enc = encrypt_secret(body.search_key)
    tenant.store().save_ai_settings({
        "provider": body.provider,
        "base_url": body.base_url,
        "model": body.model,
        "api_key_enc": api_key_enc,
        "search_provider": body.search_provider,
        "search_key_enc": search_key_enc,
        "extra": json.dumps({"temperature": body.temperature, "note": body.note}, ensure_ascii=False),
        # 密钥变更后视为需重新校验
        "validated": 0,
        "validated_at": "",
        "updated_at": utcnow(),
    })
    return MessageResponse(ok=True, message="AI 配置已保存（修改密钥后请重新「测试连接」）")


@app.post("/api/ai/settings/validate", response_model=AIValidateResponse)
def validate_ai_settings(tenant: TenantContext = Depends(get_current_tenant)):
    settings = load_settings(tenant.store())
    if not settings:
        raise HTTPException(status_code=400, detail="请先在 AI 配置中填写并保存设置")
    if settings.get("provider") != "ollama" and not settings.get("api_key"):
        raise HTTPException(status_code=400, detail="缺少 API Key，无法校验")
    ok, msg = validate_key(settings)
    row = tenant.store().get_ai_settings() or {}
    row["validated"] = 1 if ok else 0
    row["validated_at"] = utcnow() if ok else ""
    tenant.store().save_ai_settings(row)
    return AIValidateResponse(ok=ok, message=msg)


@app.delete("/api/ai/settings", response_model=MessageResponse)
def delete_ai_settings(tenant: TenantContext = Depends(get_current_tenant)):
    tenant.store().delete_ai_settings()
    return MessageResponse(ok=True, message="AI 配置（含密钥）已删除")


@app.post("/api/ai/complete", response_model=JobOut, status_code=202)
def ai_complete_endpoint(body: AICompleteRequest, tenant: TenantContext = Depends(get_current_tenant)):
    job_id = ai_job_manager().submit(tenant, "ai_complete", body.model_dump())
    return JobOut(id=job_id, business_line=body.business_line or "ai", status="queued")


@app.post("/api/ai/generate", response_model=JobOut, status_code=202)
def ai_generate_endpoint(body: AIGenerateRequest, tenant: TenantContext = Depends(get_current_tenant)):
    job_id = ai_job_manager().submit(tenant, "ai_generate", body.model_dump())
    return JobOut(id=job_id, business_line=body.business_line or "ai", status="queued")


@app.post("/api/ai/research", response_model=JobOut, status_code=202)
def ai_research_endpoint(body: AIResearchRequest, tenant: TenantContext = Depends(get_current_tenant)):
    job_id = ai_job_manager().submit(tenant, "ai_research", body.model_dump())
    return JobOut(id=job_id, business_line=body.business_line or "ai", status="queued")


# ---------------------------------------------------------------- 内部工具
def _file_sha1(path: str) -> str:
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 公开发布产物（无需鉴权，供 AI 抓取）
@app.get("/p/{bl}/{path:path}")
def serve_public(bl: str, path: str):
    bl_id = validate_id(bl, "business_line")
    full = os.path.normpath(os.path.join(PUBLISHED_DIR, bl_id, path))
    base = os.path.normpath(os.path.join(PUBLISHED_DIR, bl_id))
    if not full.startswith(base + os.sep) and full != base:
        raise HTTPException(status_code=400, detail="非法路径")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="产物不存在")
    ctype, _ = mimetypes.guess_type(full)
    return FileResponse(full, media_type=ctype or "application/octet-stream")


@app.get("/p/{bl}")
def serve_public_index(bl: str):
    bl_id = validate_id(bl, "business_line")
    d = os.path.join(PUBLISHED_DIR, bl_id)
    if not os.path.isdir(d):
        raise HTTPException(status_code=404, detail="尚未发布")
    files = sorted(os.listdir(d))
    return {"business_line": bl_id, "files": files, "base": f"{BASE_URL}/p/{bl_id}"}


# ---------------------------------------------------------------- 前端 SPA 托管
@app.get("/")
def index_root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"), media_type="text/html")


@app.get("/app")
@app.get("/app/{full_path:path}")
def index_app(full_path: str = ""):
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"), media_type="text/html")


if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
