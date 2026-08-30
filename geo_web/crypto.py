"""API 密钥安全存储 —— 对称加密（Fernet / AES-128-CBC + HMAC）。

设计：
  - 主密钥来自环境变量 GEO_MASTER_KEY；若未设置则**派生自 JWT_SECRET**（同一信任边界），
    保证任何环境都能启动且密钥不以明文落库。
  - 密钥以「密文」形式存入租户库（ai_settings 表），仅运行时解密用于调用第三方 API；
    对外接口一律返回脱敏片段（last4），杜绝密钥泄露。
  - 依赖 `cryptography`；若该库缺失，相关接口会给出明确报错提示安装，
    不影响平台其余功能。

仅做「可逆加密」（调用 API 必须还原明文），绝不哈希存储。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re

from . import JWT_SECRET

_MISSING = False
try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover - 依赖可选
    _MISSING = True
    InvalidToken = Exception  # type: ignore


def _master_key_bytes() -> bytes:
    """派生 32 字节的 Fernet 主密钥。"""
    master = os.environ.get("GEO_MASTER_KEY") or JWT_SECRET
    if not master or master == "INSECURE_DEV_SECRET_CHANGE_ME":
        # 开发环境兜底：仍加密，但提醒生产必须设置 GEO_MASTER_KEY
        master = "DEV_INSECURE_MASTER_KEY"
    return base64.urlsafe_b64encode(hashlib.sha256(master.encode("utf-8")).digest())


def _fernet():
    if _MISSING:
        raise RuntimeError(
            "缺少 cryptography 库：请执行 `pip install cryptography` 后重试"
            "（AI 密钥加密所需）。"
        )
    return Fernet(_master_key_bytes())


def encrypt_secret(plain: str) -> str:
    """把明文密钥加密为可存库的密文（base64 字符串）。"""
    if plain is None:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """解密密文为明文；空值返回空串。"""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise ValueError("密钥解密失败（主密钥不匹配或数据损坏）")


def redact(plain: str, keep: int = 4) -> str:
    """脱敏展示：仅保留前缀标识与末尾若干字符。"""
    if not plain:
        return ""
    if len(plain) <= keep + 3:
        return "****"
    head = plain[:3]
    tail = plain[-keep:]
    return f"{head}••••{tail}"


def mask_present(cipher: str) -> bool:
    """判断密文是否非空（即是否已配置密钥）。"""
    return bool(cipher)
