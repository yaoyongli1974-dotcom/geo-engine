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
