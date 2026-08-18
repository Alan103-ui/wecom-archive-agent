"""
app/services/auth/security.py — 密码哈希与 JWT 签名（纯标准库，零新依赖）

密码：PBKDF2-HMAC-SHA256（20 万次迭代，随机盐），存储格式 pbkdf2$iter$salt_hex$hash_hex
Token：HS256 JWT，负载 {sub: user_id, name, iat, exp}
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from app.config import settings

_PBKDF2_ITER = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(user_id: str, username: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": user_id,
        "name": username,
        "iat": now,
        "exp": now + settings.AUTH_TOKEN_HOURS * 3600,
    }
    seg = (
        _b64e(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    sig = hmac.new(settings.AUTH_SECRET_KEY.encode("utf-8"), seg.encode("utf-8"), hashlib.sha256).digest()
    return seg + "." + _b64e(sig)


def decode_token(token: str) -> dict | None:
    try:
        seg, sig = token.rsplit(".", 1)
        expect = hmac.new(
            settings.AUTH_SECRET_KEY.encode("utf-8"), seg.encode("utf-8"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64d(sig), expect):
            return None
        payload = json.loads(_b64d(seg.split(".")[1]))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None
