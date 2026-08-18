"""
app/models/auth.py — 平台登录认证与 RBAC 权限模型

标准 RBAC 五表：
    auth_user            用户
    auth_role            角色
    auth_permission      权限（module + action 组合成 code，如 records:delete）
    auth_user_role       用户-角色 关联
    auth_role_permission 角色-权限 关联

权限粒度：
    · 模块级：action=view（能否进入该模块 / 查看数据）
    · 按钮级：action=add / edit / delete / operate / export / config（新增、修改、删除、操作、导出、配置）
超管（is_super=True）绕过权限校验，拥有全部权限。
"""
from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _uid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now()


# 用户-角色 关联
auth_user_role = Table(
    "auth_user_role",
    Base.metadata,
    Column("user_id", String(64), ForeignKey("auth_user.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", String(64), ForeignKey("auth_role.id", ondelete="CASCADE"), primary_key=True),
)

# 角色-权限 关联
auth_role_permission = Table(
    "auth_role_permission",
    Base.metadata,
    Column("role_id", String(64), ForeignKey("auth_role.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", String(64), ForeignKey("auth_permission.id", ondelete="CASCADE"), primary_key=True),
)


class AuthUser(Base):
    __tablename__ = "auth_user"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_super: Mapped[bool] = mapped_column(Boolean, default=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    roles: Mapped[list["AuthRole"]] = relationship(
        secondary=auth_user_role, back_populates="users"
    )


class AuthRole(Base):
    __tablename__ = "auth_role"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 内置角色（admin/operator/viewer）不可删除、不可改名
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    users: Mapped[list["AuthUser"]] = relationship(
        secondary=auth_user_role, back_populates="roles"
    )
    permissions: Mapped[list["AuthPermission"]] = relationship(
        secondary=auth_role_permission, back_populates="roles"
    )


class AuthPermission(Base):
    __tablename__ = "auth_permission"
    __table_args__ = (UniqueConstraint("module", "action", name="uq_perm_module_action"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    module: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(32))
    # 形如 records:delete
    code: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    sort: Mapped[int] = mapped_column(Integer, default=0)

    roles: Mapped[list["AuthRole"]] = relationship(
        secondary=auth_role_permission, back_populates="permissions"
    )


__all__ = [
    "AuthUser",
    "AuthRole",
    "AuthPermission",
    "auth_user_role",
    "auth_role_permission",
]
