"""
app/api/delivery_config.py — 标准化「系统设置 / 通知投递」后端

把原来散落、依赖 .env 的投递通道配置，统一收口到界面可配、运行期生效、重启安全的存储：
  - SMTP 邮件：KV 存储（get/set_smtp_config）
  - 企微应用消息：wecom_config 单行（get/set_wecom_app_config）
  - 群机器人 Webhook：无全局配置，正式配置在「风险预警→规则与层」的实际管理层；
    本接口仅提供独立的「测试发送」能力。

所有读接口都不回显密码 / secret（用 has_pass / has_secret 标记），写接口对留空字段保留原值。
"""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.models.risk import AlertTarget, RiskEvent
from app.services.alert.sender import _send_one
from app.services.settings_store import (
    get_smtp_config,
    get_wecom_app_config,
    set_smtp_config,
    set_wecom_app_config,
)

router = APIRouter()

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


# ---------------------------------------------------------------- SMTP
class SmtpIn(BaseModel):
    host: str = ""
    port: int = 465
    user: str = ""
    pass_field: str = ""  # 前端字段名 pass 与 Python 关键字冲突，用 pass_field 承接
    from_addr: str = ""
    tls: bool = True


@router.get("/smtp-config", summary="读取邮件(SMTP)配置（不回显密码）")
def get_smtp():
    c = get_smtp_config()
    return {
        "host": c["host"],
        "port": c["port"],
        "user": c["user"],
        "from": c["from"],
        "tls": c["tls"],
        "has_pass": bool(c["pass"]),
    }


@router.put("/smtp-config", summary="保存邮件(SMTP)配置")
def put_smtp(payload: SmtpIn):
    if payload.from_addr and not _EMAIL_RE.match(payload.from_addr):
        raise HTTPException(status_code=400, detail="发件人邮箱地址格式不正确")
    set_smtp_config({
        "host": payload.host,
        "port": payload.port,
        "user": payload.user,
        "pass": payload.pass_field,  # 留空=保留原值
        "from": payload.from_addr,
        "tls": payload.tls,
    })
    return {"ok": True, "message": "SMTP 配置已保存"}


# ---------------------------------------------------------------- 企微应用消息
class WeComAppIn(BaseModel):
    corp_id: str = ""
    agent_id: str = ""
    agent_secret: str = ""  # 留空=保留原值


@router.get("/wecom-app-config", summary="读取企微应用消息凭证（不回显 secret）")
def get_wecom_app():
    c = get_wecom_app_config()
    return {
        "corp_id": c["corp_id"],
        "agent_id": c["agent_id"],
        "api_base_url": c["api_base_url"],
        "has_secret": bool(c["agent_secret"]),
    }


@router.put("/wecom-app-config", summary="保存企微应用消息凭证")
def put_wecom_app(payload: WeComAppIn):
    set_wecom_app_config({
        "corp_id": payload.corp_id,
        "agent_id": payload.agent_id,
        "agent_secret": payload.agent_secret,  # 留空=保留原值
    })
    return {"ok": True, "message": "企微应用消息凭证已保存"}


# ---------------------------------------------------------------- 测试发送
class DeliveryTestIn(BaseModel):
    channel: str  # webhook / app / email
    target: str   # webhook URL / touser 或 party:xxx / 收件人邮箱


@router.post("/delivery-config/test", summary="测试投递通道（构造合成事件真实发送一次）")
def test_delivery(payload: DeliveryTestIn):
    if payload.channel not in ("webhook", "app", "email"):
        return {"ok": False, "detail": f"不支持的通道 {payload.channel}"}
    if not payload.target:
        return {"ok": False, "detail": "未填写目标（URL / 接收人 / 邮箱）"}

    ev = RiskEvent(
        id="delivery-test",
        category="配置自检",
        severity="medium",
        room_id="系统设置-测试",
        from_id="system",
        snippet="这是一条来自「系统设置」页的投递测试消息，用于验证通道可达性。",
        detail="",
        biz_time=datetime.now(),
    )
    t = AlertTarget(channel=payload.channel, target=payload.target, enabled=True)
    ok, detail = _send_one(t, ev)
    return {"ok": ok, "detail": detail}
