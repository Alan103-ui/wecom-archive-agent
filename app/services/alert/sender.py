"""
app/services/alert/sender.py — 多通道预警投递（企微群 Webhook / 应用消息 / 邮件 / 系统内）

dispatch_alert() 负责把一条 RiskEvent 按路由发到对应管理层的所有投递目标，
每次发送落一条 AlertLog，失败不影响其他通道（也不抛异常）。

路由（由 pipeline 算好后传入 layer_ids）：
    - rule.alert_layers 显式指定 → 用它们
    - 否则按 severity 兜底（见 categories.DEFAULT_SEVERITY_LAYERS）
系统内通知（system）永远送达——它就是风险页上的红点，不依赖任何外部配置。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.entities import ChatRoom
from app.models.risk import AlertLog, AlertTarget, RiskEvent
from app.services.risk import categories as cat
from app.services.settings_store import get_smtp_config, get_wecom_app_config

logger = logging.getLogger(__name__)

_SEV_LABEL = {
    cat.SEVERITY_LOW: "低",
    cat.SEVERITY_MEDIUM: "中",
    cat.SEVERITY_HIGH: "高",
    cat.SEVERITY_CRITICAL: "严重",
}


def _sev_label(sev: str) -> str:
    return _SEV_LABEL.get(sev, sev)


def _room_name(room_id: str | None) -> str:
    """查群名称；查不到或失败则回退到 room_id（单聊返回「单聊」）。"""
    if not room_id:
        return "单聊"
    try:
        with SessionLocal() as db:
            room = db.get(ChatRoom, room_id)
            if room and room.name:
                return room.name
    except Exception:  # noqa: BLE001
        logger.warning("查群名称失败 room_id=%s", room_id)
    return room_id


def _build_markdown(event: RiskEvent) -> str:
    biz = event.biz_time.strftime("%Y-%m-%d %H:%M") if event.biz_time else "-"
    return (
        f"# ⚠️ 风险预警 · {event.category}\n"
        f"> **严重度**：{_sev_label(event.severity)}\n"
        f"> **群**：{_room_name(event.room_id)}\n"
        f"> **发送人**：{event.from_id or '-'}\n"
        f"> **时间**：{biz}\n\n"
        f"**命中内容**：\n> {event.snippet or '(LLM 语义命中)'}\n\n"
        f"{('**研判**：' + (event.detail or '') + '\n') if event.detail else ''}"
        f"> 请在「风控」页核查与处置"
    )


# ---------------------------------------------------------------- 通道实现
def _send_webhook(target: str, event: RiskEvent) -> tuple[bool, str]:
    if not target or not target.startswith("http"):
        return False, "Webhook 地址未配置"
    payload = {"msgtype": "markdown", "markdown": {"content": _build_markdown(event)}}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(target, json=payload)
            data = resp.json()
        if data.get("errcode") == 0:
            return True, "已推送企微群机器人"
        return False, f"企微返回 errcode={data.get('errcode')} {data.get('errmsg')}"
    except Exception as e:  # noqa: BLE001
        return False, f"Webhook 请求失败：{e}"


def _send_app(target: str, event: RiskEvent) -> tuple[bool, str]:
    cfg = get_wecom_app_config()
    token = _get_app_token(cfg)
    if not token:
        return False, "未配置企微 CORP_ID/AGENT_SECRET"
    body = {
        "msgtype": "markdown",
        "markdown": {"content": _build_markdown(event)},
    }
    if target.startswith("party:"):
        body["toparty"] = target.split(":", 1)[1]
    else:
        body["touser"] = target or "@all"
    body["agentid"] = cfg["agent_id"]
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": token},
                json=body,
            )
            d = r.json()
        if d.get("errcode") == 0:
            return True, f"已推送应用消息到 {target}"
        return False, f"应用消息返回 errcode={d.get('errcode')} {d.get('errmsg')}"
    except Exception as e:  # noqa: BLE001
        return False, f"应用消息请求失败：{e}"


def _send_email(target: str, event: RiskEvent) -> tuple[bool, str]:
    cfg = get_smtp_config()
    if not (cfg["host"] and cfg["user"] and cfg["pass"]):
        return False, "SMTP 未配置"
    if not target or "@" not in target:
        return False, "收件人邮箱未配置"
    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["from"] or cfg["user"]
        msg["To"] = target
        msg["Subject"] = f"[风险预警] {event.category}（{_sev_label(event.severity)}）"
        body = (
            f"风险分类：{event.category}\n严重度：{_sev_label(event.severity)}\n"
            f"群：{_room_name(event.room_id)}\n发送人：{event.from_id or '-'}\n"
            f"命中内容：{event.snippet or '(LLM 语义命中)'}\n"
            f"研判：{event.detail or '-'}\n\n请在风控系统核查处置。"
        )
        msg.attach(MIMEText(body, "plain", "utf-8"))

        ctx = ssl.create_default_context() if cfg["tls"] else None
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx) as s:
            s.login(cfg["user"], cfg["pass"])
            s.send_message(msg)
        return True, f"已发邮件到 {target}"
    except Exception as e:  # noqa: BLE001
        return False, f"邮件发送失败：{e}"


def _get_app_token(cfg: dict) -> str | None:
    """用运行期企微应用凭证取 access_token（重启安全）。"""
    from app.services.wecom_token import get_access_token

    return get_access_token(cfg["corp_id"], cfg["agent_secret"], cfg["api_base_url"])


def _send_system(target: str, event: RiskEvent) -> tuple[bool, str]:
    """系统内通知（风险页红点）：本地必达，不依赖任何外部配置"""
    return True, "系统内风险页通知"


_DISPATCH = {
    "webhook": _send_webhook,
    "app": _send_app,
    "email": _send_email,
    "system": _send_system,
}


def _send_one(target: AlertTarget, event: RiskEvent) -> tuple[bool, str]:
    fn = _DISPATCH.get(target.channel)
    if fn is None:
        return False, f"未知通道 {target.channel}"
    return fn(target.target, event)


# ---------------------------------------------------------------- 入口
def dispatch_alert(db: Session, event: RiskEvent, layer_ids: list[str]) -> str:
    """把事件发到指定管理层的所有启用目标。返回整体 alert_status"""
    sent = 0
    failed = 0

    # 系统内通知永远送达（这是风险页红点，不依赖外部配置）
    db.add(
        AlertLog(event_id=event.id, layer_id=None, channel="system",
                 target="in-app", status="sent", detail="系统内风险页通知")
    )
    sent += 1

    for lid in layer_ids:
        targets = db.execute(
            select(AlertTarget).where(
                AlertTarget.layer_id == lid, AlertTarget.enabled == True  # noqa: E712
            )
        ).scalars().all()
        for t in targets:
            ok, detail = _send_one(t, event)
            db.add(
                AlertLog(
                    event_id=event.id, layer_id=lid, channel=t.channel,
                    target=t.target, status="sent" if ok else "failed",
                    detail=(detail or "")[:500],
                )
            )
            sent += 1 if ok else 0
            failed += 0 if ok else 1

    if failed == 0:
        return "sent"
    return "partial" if sent > 0 else "failed"
