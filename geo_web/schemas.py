"""API 数据模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 认证
class RegisterRequest(BaseModel):
    org_name: str = Field(..., min_length=1, max_length=120, description="组织/租户名称")
    email: str = Field(..., description="owner 邮箱（同时作为登录名）")
    name: str = Field("", description="owner 显示名")
    password: str = Field(..., min_length=8, max_length=128, description="口令，≥8 位")


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class OAuthStartResponse(BaseModel):
    provider: str
    authorize_url: str
    state: str


class MessageResponse(BaseModel):
    ok: bool = True
    message: str = ""


# ---------------------------------------------------------------- 业务线
class BusinessLineIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=60, description="业务线标识（slug）")
    name: str = Field(..., min_length=1, max_length=120)
    description: str = ""
    domain: str = ""
    language: str = "zh-CN"
    topics: List[str] = Field(default_factory=list)
    audience: List[str] = Field(default_factory=list)
    competitors: List[str] = Field(default_factory=list)
    authority: Dict[str, Any] = Field(default_factory=dict)
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    targets: List[Dict[str, Any]] = Field(default_factory=list)
    llm: Dict[str, Any] = Field(default_factory=dict)
    monitor: Dict[str, Any] = Field(default_factory=dict)


class BusinessLineOut(BaseModel):
    id: str
    name: str
    description: str = ""
    domain: str = ""
    sources: int = 0
    has_content: bool = False


# ---------------------------------------------------------------- 内容 / 产物
class ContentIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., description="Markdown 或纯文本正文")
    authority: int = Field(2, ge=1, le=5)


class RunRequest(BaseModel):
    stages: Optional[List[str]] = None
    force: bool = False
    use_llm: bool = True


class JobOut(BaseModel):
    id: str
    business_line: str
    status: str
    progress: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ArtifactOut(BaseModel):
    path: str
    format: str
    checksum: str = ""


# ---------------------------------------------------------------- 内容管理扩展
class ContentIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., description="Markdown 或纯文本正文")
    authority: int = Field(2, ge=1, le=5)
    name: str = Field("", description="文件名 slug（留空则按标题自动生成）")


class ContentMeta(BaseModel):
    name: str
    title: str = ""
    authority: int = 2
    size: int = 0
    updated_at: str = ""


class ContentGet(BaseModel):
    name: str
    title: str = ""
    authority: int = 2
    content: str = ""


class ContentUpdate(BaseModel):
    name: str = Field(..., description="要更新的文件名 slug")
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., description="Markdown 或纯文本正文")
    authority: int = Field(2, ge=1, le=5)


class ContentDelete(BaseModel):
    name: str = Field(..., description="要删除的文件名 slug")


# ---------------------------------------------------------------- 业务线更新
class BusinessLineUpdate(BaseModel):
    name: str = ""
    description: str = ""
    domain: str = ""
    language: str = ""
    topics: List[str] = Field(default_factory=list)
    audience: List[str] = Field(default_factory=list)
    competitors: List[str] = Field(default_factory=list)
    authority: Dict[str, Any] = Field(default_factory=dict)
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    targets: List[Dict[str, Any]] = Field(default_factory=list)
    llm: Dict[str, Any] = Field(default_factory=dict)
    monitor: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- 发布
class PublishRequest(BaseModel):
    stages: Optional[List[str]] = None
    force: bool = True
    use_llm: bool = True


class PublishOut(BaseModel):
    job_id: str
    business_line: str
    status: str = "queued"
    urls: List[str] = Field(default_factory=list)
    published_at: Optional[str] = None


class PublishRecord(BaseModel):
    id: str
    business_line: str
    urls: List[str] = Field(default_factory=list)
    published_at: str = ""
    job_id: str = ""


# ---------------------------------------------------------------- 用户 AI 配置
class AISettingsIn(BaseModel):
    provider: str = Field("custom", description="供应商预设：openai/deepseek/moonshot/ollama/perplexity/custom")
    base_url: str = Field("", description="自定义 API Base URL（预设供应商留空则用默认）")
    model: str = Field("", description="模型名称")
    api_key: str = Field("", description="API Key；留空或 '***' 表示保留已有密钥不改")
    search_provider: str = Field("none", description="联网搜索：none/tavily/brave/native")
    search_key: str = Field("", description="搜索 API Key；留空或 '***' 表示保留已有")
    temperature: float = Field(0.7, ge=0.0, le=1.5)
    note: str = ""


class AISettingsOut(BaseModel):
    provider: str = ""
    base_url: str = ""
    model: str = ""
    has_key: bool = False
    api_key_masked: str = ""
    search_provider: str = "none"
    has_search_key: bool = False
    search_key_masked: str = ""
    validated: bool = False
    validated_at: str = ""
    temperature: float = 0.7
    note: str = ""
    presets: List[Dict[str, str]] = Field(default_factory=list)


class AIValidateResponse(BaseModel):
    ok: bool
    message: str


class AICompleteRequest(BaseModel):
    business_line: str = ""
    text: str = Field(..., description="待完善/续写的原文")
    instruction: str = ""


class AIGenerateRequest(BaseModel):
    business_line: str = ""
    topic: str = Field(..., description="内容主题")
    tone: str = "专业严谨"
    length: str = "中等"


class AIResearchRequest(BaseModel):
    business_line: str = ""
    query: str = Field(..., description="联网搜索与调研的问题")
