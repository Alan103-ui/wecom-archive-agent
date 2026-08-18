"""
app/api/users.py — 用户管理 CRUD（权限：users:view / add / edit / delete）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.auth import AuthRole, AuthUser
from app.services.auth.rbac import require_auth, require_perm
from app.services.auth.security import hash_password
from app.services.auth.policy import check_password_strength, log_audit

router = APIRouter(prefix="/users", tags=["用户管理"], dependencies=[Depends(require_auth)])


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=128)
    is_active: bool = True
    role_ids: list[str] = Field(default_factory=list)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None
    role_ids: list[str] | None = None


class UserResetPwd(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


def _user_out(db: Session, user: AuthUser) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "is_active": user.is_active,
        "is_super": user.is_super,
        "roles": [
            {"id": r.id, "name": r.name, "code": r.code}
            for r in sorted(user.roles, key=lambda x: x.code)
        ],
        "last_login_at": user.last_login_at,
        "remark": user.remark,
        "created_at": user.created_at,
    }


def _load_roles(db: Session, role_ids: list[str]) -> list[AuthRole]:
    if not role_ids:
        return []
    roles = db.execute(select(AuthRole).where(AuthRole.id.in_(role_ids))).scalars().all()
    if len(roles) != len(set(role_ids)):
        raise HTTPException(400, "包含不存在的角色")
    return list(roles)


@router.get("", summary="用户列表")
def list_users(db: Session = Depends(get_db), _=Depends(require_perm("users", "view"))):
    users = db.execute(select(AuthUser).order_by(AuthUser.created_at)).scalars().all()
    return [_user_out(db, u) for u in users]


@router.post("", summary="新增用户")
def create_user(payload: UserIn, db: Session = Depends(get_db), _=Depends(require_perm("users", "add"))):
    exists = db.execute(select(AuthUser).where(AuthUser.username == payload.username)).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"用户名已存在：{payload.username}")
    ok, msg = check_password_strength(payload.password)
    if not ok:
        raise HTTPException(400, msg)
    user = AuthUser(
        username=payload.username,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        is_active=payload.is_active,
    )
    user.roles = _load_roles(db, payload.role_ids)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "用户名已存在")
    db.refresh(user)
    return _user_out(db, user)


@router.patch("/{user_id}", summary="修改用户（资料/状态/角色）")
def update_user(
    user_id: str,
    payload: UserPatch,
    db: Session = Depends(get_db),
    _=Depends(require_perm("users", "edit")),
):
    user = db.get(AuthUser, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    data = payload.model_dump(exclude_unset=True)
    if "display_name" in data:
        user.display_name = data["display_name"] or ""
    if "is_active" in data and data["is_active"] is not None:
        # 防止把超管停用（避免系统失去管理员入口）
        if user.is_super and not data["is_active"]:
            raise HTTPException(400, "不能停用超级管理员账号")
        user.is_active = data["is_active"]
    if "role_ids" in data and data["role_ids"] is not None:
        if user.is_super:
            raise HTTPException(400, "超级管理员不通过角色授权，无需分配角色")
        user.roles = _load_roles(db, data["role_ids"])
    db.commit()
    db.refresh(user)
    return _user_out(db, user)


@router.delete("/{user_id}", summary="删除用户")
def delete_user(user_id: str, db: Session = Depends(get_db), _=Depends(require_perm("users", "delete"))):
    user = db.get(AuthUser, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    if user.is_super:
        raise HTTPException(400, "超级管理员不可删除")
    db.delete(user)
    db.commit()
    return {"ok": True, "message": "已删除用户"}


@router.post("/{user_id}/reset-password", summary="重置用户密码")
def reset_password(
    user_id: str,
    payload: UserResetPwd,
    db: Session = Depends(get_db),
    _=Depends(require_perm("users", "edit")),
):
    user = db.get(AuthUser, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    ok, msg = check_password_strength(payload.new_password)
    if not ok:
        raise HTTPException(400, msg)
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    log_audit("user_crud", user.username, detail="重置密码")
    return {"ok": True, "message": "密码已重置"}
