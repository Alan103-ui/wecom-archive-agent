"""
app/services/auth/rbac.py — 认证与权限 FastAPI 依赖

用法：
    router = APIRouter(dependencies=[Depends(require_auth)])          # 该路由下全部接口需登录
    @router.delete("/records/{id}")
    def delete_record(..., _: AuthUser = Depends(require_perm("records", "delete"))):
        ...
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.auth import (
    AuthPermission,
    AuthRole,
    AuthUser,
    auth_role_permission,
    auth_user_role,
)
from app.services.auth.security import decode_token

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AuthUser:
    """从 Authorization: Bearer <token> 解析当前登录用户。"""
    if cred is None or not cred.credentials:
        raise HTTPException(401, "未登录，请先登录")
    payload = decode_token(cred.credentials)
    if not payload:
        raise HTTPException(401, "登录已过期，请重新登录")
    user = db.get(AuthUser, payload.get("sub"))
    if user is None:
        raise HTTPException(401, "用户不存在")
    if not user.is_active:
        raise HTTPException(403, "账号已停用，请联系管理员")
    return user


def require_auth(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """仅要求登录。"""
    return user


def user_perm_codes(db: Session, user: AuthUser) -> set[str]:
    """当前用户全部权限码；超管返回 {'*'}。"""
    if user.is_super:
        return {"*"}
    rows = db.execute(
        select(AuthPermission.code)
        .join(auth_role_permission, auth_role_permission.c.permission_id == AuthPermission.id)
        .join(AuthRole, AuthRole.id == auth_role_permission.c.role_id)
        .join(auth_user_role, auth_user_role.c.role_id == AuthRole.id)
        .where(auth_user_role.c.user_id == user.id)
    ).scalars().all()
    return set(rows)


def require_perm(module: str, action: str):
    """按钮/模块级权限依赖：require_perm('records', 'delete') 要求 records:delete 权限。"""
    code = f"{module}:{action}"

    def _dep(
        user: AuthUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> AuthUser:
        codes = user_perm_codes(db, user)
        if "*" in codes or code in codes:
            return user
        raise HTTPException(403, f"无权限执行该操作（缺少权限：{code}）")

    return _dep
