"""
app/api/__init__.py — 路由聚合

认证策略：
    · /api/auth/* 开放（登录接口本身无需登录）
    · 其余全部业务路由挂登录依赖（dependencies=[Depends(require_auth)]）
    · 具体「模块/按钮」权限（如 records:delete）在各接口上用 require_perm 精确声明
"""
from fastapi import APIRouter, Depends

from app.api import (
    attachments,
    auth,
    delivery_config,
    extract as extract_api,
    license as license_api,
    messages,
    models,
    permissions,
    records,
    risks,
    roles,
    rooms,
    settings as settings_api,
    system,
    templates,
    users,
    wecom,
    wecom_config,
)
from app.services.auth.rbac import require_auth

api_router = APIRouter()

# 认证（登录公开，其余接口内部按 require_auth 控制）
api_router.include_router(auth.router, tags=["认证"])

# 平台管理（用户/角色/权限目录/授权 License）
api_router.include_router(users.router, tags=["用户管理"])
api_router.include_router(roles.router, tags=["角色管理"])
api_router.include_router(permissions.router, tags=["权限管理"])
api_router.include_router(license_api.router, tags=["授权管理"])

# 业务路由：全部要求登录（prefix 传空串=不额外加前缀）
_biz_routers = [
    (system.router, "/system", ["系统"]),
    (messages.router, "", ["消息"]),
    (attachments.router, "", ["附件与OCR"]),
    (records.router, "", ["结构化数据"]),
    (templates.router, "", ["抽取模板"]),
    (risks.router, "/risks", ["风险预警"]),
    (models.router, "/models", ["模型配置"]),
    (rooms.router, "/rooms", ["群管理"]),
    (settings_api.router, "/settings", ["设置"]),
    (wecom_config.router, "", ["企业微信配置"]),
    (delivery_config.router, "", ["投递配置"]),
    (wecom.router, "/wecom", ["企业微信辅助接口"]),
    (extract_api.router, "/extract", ["抽取对比"]),
]
for router, prefix, tags in _biz_routers:
    api_router.include_router(
        router,
        prefix=prefix,
        tags=tags,
        dependencies=[Depends(require_auth)],
    )

__all__ = ["api_router"]
