"""可插拔 OAuth 身份提供方。

已实现：
  - WeComProvider  ：企业微信（内部应用）OAuth2 登录
  - DevProvider    ：开发/测试用，免网络返回确定性身份（便于联调与单测）

扩展新提供方（GitHub / 微信开放平台 / OIDC …）只需继承 OAuthProvider 并实现
authorize_url() 与 exchange()，再到 PROVIDERS 工厂登记即可，不改变任何调用方。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional

from . import BASE_URL


@dataclass
class ExternalIdentity:
    """OAuth 交换后归一化的外部身份。"""
    provider: str
    external_id: str
    email: Optional[str] = None
    name: Optional[str] = None


class OAuthProvider(ABC):
    name = "base"

    @abstractmethod
    def authorize_url(self, state: str, redirect_uri: Optional[str] = None) -> str:
        ...

    @abstractmethod
    def exchange(self, code: str, redirect_uri: Optional[str] = None) -> ExternalIdentity:
        ...


class WeComProvider(OAuthProvider):
    """企业微信（内部应用）网页授权登录。

    流程：
      1) authorize_url 跳转企业微信扫码/授权页（scope=snsapi_userinfo）
      2) 回调带 code，exchange 中：
         a. gettoken(corpid, corpsecret) -> access_token
         b. auth/getuserinfo(access_token, code) -> userid
         c. user/get(access_token, userid) -> name, email
    未配置 corpid/corpsecret 或 test_mode=True 时，返回由 code 派生的确定性
    测试身份，便于无凭据联调与自动化测试。
    """

    name = "wecom"

    def __init__(self, corpid: str = "", corpsecret: str = "",
                 agentid: str = "", test_mode: bool = False) -> None:
        self.corpid = corpid
        self.corpsecret = corpsecret
        self.agentid = agentid
        self.test_mode = test_mode or not (corpid and corpsecret)

    def authorize_url(self, state: str, redirect_uri: Optional[str] = None) -> str:
        redirect = redirect_uri or f"{BASE_URL}/api/auth/oauth/wecom/callback"
        q = {
            "appid": self.corpid,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": "snsapi_userinfo",
            "state": state,
        }
        return ("https://open.weixin.qq.com/connect/oauth2/authorize?"
                + urllib.parse.urlencode(q) + "#wechat_redirect")

    # ---- 内部 HTTP 助手 ----
    @staticmethod
    def _get_json(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "geo-web/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec - 白名单域名
            return json.loads(resp.read().decode("utf-8"))

    def exchange(self, code: str, redirect_uri: Optional[str] = None) -> ExternalIdentity:
        if self.test_mode:
            # 确定性测试身份：external_id 由 code 派生，email 归一
            ext = "test-" + (code or "demo")
            return ExternalIdentity(
                provider=self.name, external_id=ext,
                email=f"{ext}@wecom.test", name=f"企业微信测试用户({ext})")

        token_url = (f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?"
                     f"corpid={self.corpid}&corpsecret={self.corpsecret}")
        tok = self._get_json(token_url)
        if tok.get("errcode", 0) != 0:
            raise RuntimeError(f"企业微信获取 access_token 失败: {tok}")
        access_token = tok["access_token"]

        info_url = (f"https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo?"
                    f"access_token={access_token}&code={urllib.parse.quote(code)}")
        info = self._get_json(info_url)
        if info.get("errcode", 0) != 0:
            raise RuntimeError(f"企业微信获取 userinfo 失败: {info}")
        userid = info.get("UserId") or info.get("userid")
        if not userid:
            raise RuntimeError("企业微信未返回 userid（可能 scope 不含用户信息）")

        user_url = (f"https://qyapi.weixin.qq.com/cgi-bin/user/get?"
                    f"access_token={access_token}&userid={urllib.parse.quote(userid)}")
        user = self._get_json(user_url)
        if user.get("errcode", 0) != 0:
            raise RuntimeError(f"企业微信获取用户详情失败: {user}")
        email = user.get("email") or f"{userid}@{self.corpid}.wecom"
        name = user.get("name") or userid
        return ExternalIdentity(provider=self.name, external_id=userid,
                                email=email, name=name)


class DevProvider(OAuthProvider):
    """开发用 Mock：免去任何外部依赖，给定 code 即返回固定测试身份。"""
    name = "dev"

    def __init__(self, **_kw: object) -> None:
        pass

    def authorize_url(self, state: str, redirect_uri: Optional[str] = None) -> str:
        redirect = redirect_uri or f"{BASE_URL}/api/auth/oauth/dev/callback"
        return f"{redirect}?state={urllib.parse.quote(state)}&code=dev-{state}"

    def exchange(self, code: str, redirect_uri: Optional[str] = None) -> ExternalIdentity:
        ext = code or "dev-demo"
        return ExternalIdentity(provider=self.name, external_id=ext,
                                email=f"{ext}@dev.local", name=f"Dev({ext})")


# ---------------------------------------------------------------- 工厂
PROVIDERS: Dict[str, type] = {
    "wecom": WeComProvider,
    "dev": DevProvider,
}


def get_provider(name: str, config: Optional[Dict[str, str]] = None) -> OAuthProvider:
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"未知的 OAuth 提供方: {name}（可选：{', '.join(PROVIDERS)}）")
    cfg = config or {}
    return cls(**cfg)
