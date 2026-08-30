"""认证核心：JWT（HS256，纯标准库）与密码哈希（PBKDF2-HMAC-SHA256）。

说明：
  - 为保持「零额外依赖即可运行」，JWT 与密码哈希均用标准库实现；
  - 生产环境可用 PyJWT / argon2-cffi 替换，对外接口（sign/verify/hash/verify_pw）
    保持一致即可，无需改动调用方。
  - 口令采用 PBKDF2-HMAC-SHA256（20 万次迭代）+ 每用户随机盐，优于明文/弱哈希；
    若追求更强抗 GPU 破解，建议升级 argon2id（接口不变）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional

from . import ACCESS_TOKEN_MIN, JWT_SECRET, REFRESH_TOKEN_DAYS


# ---------------------------------------------------------------- 基础编解码
def _b64u(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------- JWT (HS256)
def sign_jwt(payload: Dict[str, Any], secret: str = JWT_SECRET,
             exp_min: int = ACCESS_TOKEN_MIN) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    body = dict(payload)
    now = int(time.time())
    body["iat"] = now
    body["exp"] = now + exp_min * 60
    seg = _b64u(json.dumps(header, separators=(",", ":")).encode()) + b"." + \
        _b64u(json.dumps(body, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), seg, hashlib.sha256).digest()
    return (seg + b"." + _b64u(sig)).decode()


def verify_jwt(token: str, secret: str = JWT_SECRET) -> Dict[str, Any]:
    try:
        h, p, s = token.split(".")
    except ValueError:
        raise ValueError("JWT 格式错误")
    expected = hmac.new(secret.encode(), (h + "." + p).encode(),
                        hashlib.sha256).digest()
    if not hmac.compare_digest(_b64u(expected), s.encode()):
        raise ValueError("JWT 签名校验失败")
    body = json.loads(_b64d(p))
    if body.get("exp", 0) < int(time.time()):
        raise ValueError("JWT 已过期")
    return body


# ---------------------------------------------------------------- 密码哈希
def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2$sha256${iterations}${_b64u(salt).decode()}${_b64u(dk).decode()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or "$" not in stored:
        return False
    try:
        alg, hashname, iters_s, salt_s, dk_s = stored.split("$")
    except ValueError:
        return False
    if alg != "pbkdf2":
        return False
    try:
        salt = _b64d(salt_s)
        expected = _b64d(dk_s)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac(hashname, password.encode("utf-8"), salt, int(iters_s))
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------- 令牌工具
def gen_token_id() -> str:
    return secrets.token_urlsafe(24)


def refresh_expiry() -> str:
    """返回 refresh 令牌过期时间戳（天）。"""
    return str(int(time.time()) + REFRESH_TOKEN_DAYS * 86400)
