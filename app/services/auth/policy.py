"""
app/services/auth/policy.py — 密码策略 + 登录失败锁定 + 审计日志（纯标准库）

- 密码强度：最小长度（默认 8）+ 必须同时含字母与数字
- 登录锁定：连续失败 N 次锁 M 分钟（进程内计数；本系统单进程部署，重启清零可接受）
- 审计日志：登录成功/失败/锁定/改密等安全事件写入 AUDIT_LOG_PATH（data/audit.log）
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger("app.security")


# ----------------------------------------------------------------------------
# 密码强度
# ----------------------------------------------------------------------------
def check_password_strength(pwd: str) -> tuple[bool, str]:
    if len(pwd) < settings.AUTH_PASSWORD_MIN_LEN:
        return False, f"密码至少 {settings.AUTH_PASSWORD_MIN_LEN} 位"
    if not re.search(r"[A-Za-z]", pwd) or not re.search(r"\d", pwd):
        return False, "密码需同时包含字母和数字"
    return True, ""


# ----------------------------------------------------------------------------
# 审计日志
# ----------------------------------------------------------------------------
_audit_path: Path | None = None


def _audit_file() -> Path:
    global _audit_path
    if _audit_path is None:
        _audit_path = Path(settings.AUDIT_LOG_PATH)
        _audit_path.parent.mkdir(parents=True, exist_ok=True)
    return _audit_path


def log_audit(event: str, username: str, ip: str = "", detail: str = "") -> None:
    """追加一条审计日志（失败不抛出，仅记录）。event 示例：login_success/login_failed/login_blocked/change_password/user_crud"""
    try:
        line = f"{datetime.now().isoformat(timespec='seconds')} | {event} | {username} | {ip} | {detail}\n"
        with _audit_file().open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        logger.warning("审计日志写入失败 event=%s", event, exc_info=True)


# ----------------------------------------------------------------------------
# 登录失败锁定（进程内）
# ----------------------------------------------------------------------------
_fail_store: dict[str, dict] = {}  # username -> {fails, lock_until_ts}


def is_locked(username: str) -> tuple[bool, int]:
    """返回 (是否锁定, 剩余锁定分钟数)"""
    cur = _fail_store.get(username)
    if not cur or cur.get("lock_until", 0.0) <= time.time():
        return False, 0
    remain = int(cur["lock_until"] - time.time()) // 60 + 1
    return True, remain


def register_login_failure(username: str) -> None:
    cur = _fail_store.setdefault(username, {"fails": 0, "lock_until": 0.0})
    if cur["lock_until"] > time.time():
        return  # 已锁定，不再累加
    cur["fails"] += 1
    if cur["fails"] >= settings.AUTH_MAX_FAILS:
        cur["lock_until"] = time.time() + settings.AUTH_LOCK_MINUTES * 60


def reset_login_failures(username: str) -> None:
    _fail_store.pop(username, None)
