"""
app/api/roles.py — 角色管理 CRUD + 权限分配（权限：roles:view / add / edit / delete）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.auth import AuthPermission, AuthRole, AuthUser
from app.services.auth.rbac import require_auth, require_perm

router = APIRouter(prefix="/roles", tags=["角色管理"], dependencies=[Depends(require_auth)])


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    description: str | None = None
    permission_ids: list[str] = Field(default_factory=list)


class RolePatch(BaseModel):
    description: str | None = None
    permission_ids: list[str] | None = None


def _role_out(role: AuthRole, user_count: int) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "code": role.code,
        "description": role.description,
        "is_builtin": role.is_builtin,
        "user_count": user_count,
        "permission_ids": [p.id for p in role.permissions],
        "permission_codes": sorted(p.code for p in role.permissions),
        "created_at": role.created_at,
    }


@router.get("", summary="角色列表（含权限与成员数）")
def list_roles(db: Session = Depends(get_db), _=Depends(require_perm("roles", "view"))):
    roles = db.execute(select(AuthRole).order_by(AuthRole.code)).scalars().all()
    out = []
    for r in roles:
        cnt = db.execute(
            select(AuthUser.id).where(AuthUser.roles.any(AuthRole.id == r.id))
        ).scalars().all()
        out.append(_role_out(r, len(cnt)))
    return out


@router.post("", summary="新增角色")
def create_role(payload: RoleIn, db: Session = Depends(get_db), _=Depends(require_perm("roles", "add"))):
    if db.execute(select(AuthRole).where(AuthRole.code == payload.code)).scalar_one_or_none():
        raise HTTPException(409, f"角色编码已存在：{payload.code}")
    role = AuthRole(name=payload.name, code=payload.code, description=payload.description)
    role.permissions = _load_perms(db, payload.permission_ids)
    db.add(role)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "角色名称或编码已存在")
    db.refresh(role)
    return _role_out(role, 0)


@router.patch("/{role_id}", summary="修改角色（名称/说明/权限分配）")
def update_role(
    role_id: str,
    payload: RolePatch,
    db: Session = Depends(get_db),
    _=Depends(require_perm("roles", "edit")),
):
    role = db.get(AuthRole, role_id)
    if role is None:
        raise HTTPException(404, "角色不存在")
    if role.code == "admin":
        raise HTTPException(400, "超级管理员为系统内置角色，无需也不能修改权限")
    if payload.description is not None:
        role.description = payload.description
    if payload.permission_ids is not None:
        if role.code in ("operator", "viewer"):
            # 内置业务角色允许微调权限，但仅限"非系统级"权限已在目录层约束，这里直接按所选保存
            pass
        role.permissions = _load_perms(db, payload.permission_ids)
    db.commit()
    db.refresh(role)
    cnt = len(db.execute(
        select(AuthUser.id).where(AuthUser.roles.any(AuthRole.id == role.id))
    ).scalars().all())
    return _role_out(role, cnt)


@router.delete("/{role_id}", summary="删除角色")
def delete_role(role_id: str, db: Session = Depends(get_db), _=Depends(require_perm("roles", "delete"))):
    role = db.get(AuthRole, role_id)
    if role is None:
        raise HTTPException(404, "角色不存在")
    if role.is_builtin:
        raise HTTPException(400, "内置角色不可删除")
    users = db.execute(select(AuthUser.id).where(AuthUser.roles.any(AuthRole.id == role.id))).scalars().all()
    if users:
        raise HTTPException(400, f"仍有 {len(users)} 个用户绑定该角色，请先移除")
    db.delete(role)
    db.commit()
    return {"ok": True, "message": "已删除角色"}


def _load_perms(db: Session, permission_ids: list[str]) -> list[AuthPermission]:
    if not permission_ids:
        return []
    perms = db.execute(
        select(AuthPermission).where(AuthPermission.id.in_(permission_ids))
    ).scalars().all()
    if len(perms) != len(set(permission_ids)):
        raise HTTPException(400, "包含不存在的权限")
    return list(perms)
