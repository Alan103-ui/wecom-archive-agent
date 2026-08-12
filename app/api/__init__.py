"""
app/api/__init__.py — 路由聚合
"""
from fastapi import APIRouter

from app.api import (
    attachments,
    extract as extract_api,
    messages,
    models,
    records,
    risks,
    rooms,
    settings as settings_api,
    system,
    templates,
    wecom,
    wecom_config,
    delivery_config,
)

api_router = APIRouter()
api_router.include_router(system.router, prefix="/system", tags=["系统"])
api_router.include_router(messages.router, tags=["消息"])
api_router.include_router(attachments.router, tags=["附件与OCR"])
api_router.include_router(records.router, tags=["结构化数据"])
api_router.include_router(templates.router, tags=["抽取模板"])
api_router.include_router(risks.router, prefix="/risks", tags=["风险预警"])
api_router.include_router(models.router, prefix="/models", tags=["模型配置"])
api_router.include_router(rooms.router, prefix="/rooms", tags=["群管理"])
api_router.include_router(settings_api.router, prefix="/settings", tags=["设置"])
api_router.include_router(wecom_config.router, tags=["企业微信配置"])
api_router.include_router(delivery_config.router, tags=["投递配置"])
api_router.include_router(wecom.router, prefix="/wecom", tags=["企业微信辅助接口"])
api_router.include_router(extract_api.router, prefix="/extract", tags=["抽取对比"])

__all__ = ["api_router"]
