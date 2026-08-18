"""
app/services/auth/catalog.py — 权限目录与种子数据（幂等）

权限目录 = 平台全部「模块 × 按钮」清单，是角色分配权限时的勾选项来源。
内置角色：
    admin    超级管理员（is_super=True，绕过校验，不依赖权限表）
    operator 运营专员（全部业务查看+操作，但不能动系统凭证与用户/角色/权限）
    viewer   只读用户（各模块仅 view）
默认账号：admin / admin123（首次登录后请立即修改密码）
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import AuthPermission, AuthRole, AuthUser
from app.services.auth.security import hash_password

# 模块 → {名称, 按钮权限 → 中文名}
PERMISSION_CATALOG: dict[str, dict] = {
    "dashboard": {"name": "总览", "actions": {"view": "查看总览"}},
    "rooms": {"name": "群管理", "actions": {
        "view": "查看群列表", "edit": "采集开关/备注修改", "delete": "删除群及数据"}},
    "messages": {"name": "聊天记录", "actions": {
        "view": "查看消息", "delete": "删除消息"}},
    "attachments": {"name": "附件与OCR", "actions": {
        "view": "查看附件/OCR", "operate": "重跑 OCR/抽取"}},
    "records": {"name": "结构化数据", "actions": {
        "view": "查看记录", "edit": "人工复核/修正", "delete": "删除记录", "export": "导出 Excel"}},
    "templates": {"name": "抽取模板", "actions": {
        "view": "查看模板", "add": "新增模板", "edit": "修改模板", "delete": "删除模板"}},
    "risks": {"name": "风险预警", "actions": {
        "view": "查看风险", "operate": "处置/重发/重扫", "config": "配置规则/层级/目标"}},
    "models": {"name": "模型配置", "actions": {
        "view": "查看模型", "add": "新增模型", "edit": "修改模型", "delete": "删除模型",
        "operate": "测试/拉取清单"}},
    "settings": {"name": "消息设置", "actions": {
        "view": "查看设置", "edit": "修改设置"}},
    "wecom": {"name": "企业微信接口", "actions": {
        "view": "查看配置", "edit": "保存配置", "operate": "验证凭证"}},
    "delivery": {"name": "投递配置", "actions": {
        "view": "查看配置", "edit": "保存配置", "operate": "测试投递"}},
    "extract": {"name": "抽取对比", "actions": {
        "view": "查看对比", "operate": "切换模式"}},
    "system": {"name": "系统运维", "actions": {
        "view": "查看状态", "operate": "同步/调度/游标"}},
    "users": {"name": "用户管理", "actions": {
        "view": "查看用户", "add": "新增用户", "edit": "修改用户", "delete": "删除用户"}},
    "roles": {"name": "角色管理", "actions": {
        "view": "查看角色", "add": "新增角色", "edit": "修改角色/分配权限", "delete": "删除角色"}},
    "permissions": {"name": "权限目录", "actions": {"view": "查看权限"}},
}

# 不允许被「运营/普通角色」触碰的系统级模块（仅超管可编辑）
_SYSTEM_MODULES = {"system", "users", "roles", "permissions", "wecom", "delivery", "settings"}

# 运营专员在「全模块 view」基础上额外授予的业务按钮权限
_OPERATOR_EXTRA = {
    "rooms:edit", "rooms:delete",
    "messages:delete",
    "attachments:operate",
    "records:edit", "records:delete", "records:export",
    "templates:add", "templates:edit", "templates:delete",
    "risks:operate", "risks:config",
    "models:add", "models:edit", "models:delete", "models:operate",
    "extract:operate",
}

_BUILTIN_ROLES: dict[str, dict] = {
    "admin": {
        "name": "超级管理员",
        "desc": "拥有全部权限（含系统管理、用户与角色管理），可执行一切操作",
        "super": True,
    },
    "operator": {
        "name": "运营专员",
        "desc": "全部业务查看与操作（新增/修改/删除/导出），但不可修改系统凭证、用户与角色",
        "super": False,
    },
    "viewer": {
        "name": "只读用户",
        "desc": "仅可查看各模块数据，无任何新增/修改/删除/操作权限",
        "super": False,
    },
}


def seed_auth(db: Session) -> None:
    """幂等播种：权限目录 → 内置角色 → 默认管理员。可安全重复调用。"""
    # 1) 权限目录
    perms_by_code: dict[str, AuthPermission] = {
        p.code: p for p in db.execute(select(AuthPermission)).scalars()
    }
    sort = 0
    for module, meta in PERMISSION_CATALOG.items():
        for action, name in meta["actions"].items():
            code = f"{module}:{action}"
            p = perms_by_code.get(code)
            if p is None:
                p = AuthPermission(module=module, action=action, code=code, name=name, sort=sort)
                db.add(p)
                perms_by_code[code] = p
            else:
                p.name = name
            sort += 1
    db.flush()

    # 2) 内置角色
    roles_by_code: dict[str, AuthRole] = {r.code: r for r in db.execute(select(AuthRole)).scalars()}
    view_codes = [c for c in perms_by_code if c.endswith(":view")]
    for code, spec in _BUILTIN_ROLES.items():
        role = roles_by_code.get(code)
        if role is None:
            role = AuthRole(name=spec["name"], code=code, description=spec["desc"], is_builtin=True)
            db.add(role)
            roles_by_code[code] = role
        role.is_builtin = True
        if spec["super"]:
            role.permissions = []  # 超管不依赖权限表
            continue
        if code == "operator":
            wanted = set(view_codes) | _OPERATOR_EXTRA
        else:  # viewer
            wanted = set(view_codes)
        role.permissions = [perms_by_code[c] for c in sorted(wanted)]
    db.flush()

    # 3) 默认管理员（admin / admin123）
    admin = db.execute(select(AuthUser).where(AuthUser.username == "admin")).scalar_one_or_none()
    if admin is None:
        admin = AuthUser(
            username="admin",
            password_hash=hash_password("admin123"),
            display_name="系统管理员",
            is_super=True,
            is_active=True,
        )
        admin.roles.append(roles_by_code["admin"])
        db.add(admin)
    else:
        # 管理员账号必须保持超管（防止被误改）
        admin.is_super = True

    db.commit()
