"""
app/api/auth.py — 登录 / 当前用户 / 修改密码
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.auth import AuthUser
from app.services.auth.policy import (
    check_password_strength,
    is_locked,
    log_audit,
    register_login_failure,
    reset_login_failures,
)
from app.services.auth.rbac import require_auth, user_perm_codes
from app.services.auth.security import create_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["认证"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/login", summary="登录")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else ""

    # 登录失败锁定：连续失败达到阈值后锁定一段时间
    locked, remain = is_locked(payload.username)
    if locked:
        log_audit("login_blocked", payload.username, ip, f"账号锁定中，剩余约 {remain} 分钟")
        raise HTTPException(429, f"失败次数过多，账号已锁定约 {remain} 分钟，请稍后再试")

    user = db.execute(select(AuthUser).where(AuthUser.username == payload.username)).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        register_login_failure(payload.username)
        log_audit("login_failed", payload.username, ip, "用户名或密码错误")
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已停用，请联系管理员")

    reset_login_failures(payload.username)
    user.last_login_at = datetime.now()
    db.commit()
    log_audit("login_success", payload.username, ip)
    return {
        "token": create_token(user.id, user.username),
        "user": _user_brief(user, db),
    }


def _user_brief(user: AuthUser, db: Session) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "is_super": user.is_super,
        "perms": sorted(user_perm_codes(db, user)),
    }


@router.get("/me", summary="当前登录用户信息")
def me(user: AuthUser = Depends(require_auth), db: Session = Depends(get_db)):
    return _user_brief(user, db)


class ChangePwdIn(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/change-password", summary="修改自己的密码")
def change_password(
    payload: ChangePwdIn,
    request: Request,
    user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else ""
    if not verify_password(payload.old_password, user.password_hash):
        log_audit("change_password_failed", user.username, ip, "原密码错误")
        raise HTTPException(400, "原密码不正确")
    if payload.old_password == payload.new_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    ok, msg = check_password_strength(payload.new_password)
    if not ok:
        raise HTTPException(400, msg)
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    log_audit("change_password", user.username, ip)
    return {"ok": True, "message": "密码已修改，下次登录请使用新密码"}
