"""
app/api/auth.py — 登录 / 当前用户 / 修改密码
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.auth import AuthUser
from app.services.auth.rbac import require_auth, user_perm_codes
from app.services.auth.security import create_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["认证"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/login", summary="登录")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.execute(select(AuthUser).where(AuthUser.username == payload.username)).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已停用，请联系管理员")
    user.last_login_at = datetime.now()
    db.commit()
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
    new_password: str = Field(min_length=6, max_length=128)


@router.post("/change-password", summary="修改自己的密码")
def change_password(
    payload: ChangePwdIn,
    user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(400, "原密码不正确")
    if payload.old_password == payload.new_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True, "message": "密码已修改，下次登录请使用新密码"}
