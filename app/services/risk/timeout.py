"""
app/services/risk/timeout.py — 超时回复提醒（会话时间线聚合）

思路：逐群按时间排序消息，识别"客户消息连续出现、且其后没有员工回复"的最近一段，
若这段的起始（第一条客户消息）距今已超过阈值，则生成一条「服务响应超时」风险事件。

判定要点：
  - 员工 vs 客户：优先用群的成员列表（企业成员=员工），否则按发送方 id 前缀兜底
    （wo/wm 开头视为外部联系人=客户；wb 机器人按员工处理，避免误触发）。
  - 仅处理群聊（room_id 非空）且群内有过员工消息的群，避免对纯内部群/纯客户群误报。
  - 仅当群内存在员工消息（has_emp）时才可能触发，确保是"被监控的服务群"。
  - 去重：以"第一条未回复客户消息 id + 分类"唯一约束，重复扫描不会重复建事件。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.entities import ChatMessage, ChatRoom
from app.models.risk import RiskEvent
from app.services.alert import sender
from app.services.risk import categories as cat

logger = logging.getLogger(__name__)

_KV_KEY = "risk_timeout"


def _looks_external(from_id: str) -> bool:
    """外部联系人兜底判断：wo/wm 开头视为客户（不含 wb 机器人）。"""
    return bool(from_id) and from_id[:2] in ("wo", "wm")


def _is_employee(from_id: str, members: set[str]) -> bool:
    """员工判定：有成员列表则看是否在列；否则按前缀兜底（非 wo/wm 即员工）。"""
    if members:
        return from_id in members
    return not _looks_external(from_id)


def _load_cfg(db: Session) -> tuple[bool, int, str]:
    """读取超时配置：KV 覆盖优先，回落到 config 默认值。"""
    enabled = settings.RISK_TIMEOUT_ENABLED
    minutes = settings.RISK_TIMEOUT_MINUTES
    severity = settings.RISK_TIMEOUT_SEVERITY
    try:
        from app.models.kv import KVSetting

        row = db.get(KVSetting, _KV_KEY)
        if row and isinstance(row.value_json, dict):
            v = row.value_json
            if "enabled" in v:
                enabled = bool(v["enabled"])
            if v.get("minutes"):
                try:
                    minutes = int(v["minutes"])
                except (TypeError, ValueError):
                    pass
            if v.get("severity") in cat.SEVERITY_ORDER:
                severity = v["severity"]
    except Exception as e:  # noqa: BLE001
        logger.warning("读取超时配置失败，回落默认：%s", e)
    return enabled, minutes, severity


def _scan_room(db: Session, room: ChatRoom, minutes: int, severity: str, now: datetime) -> RiskEvent | None:
    """扫描单个群，若发现超时未回复则返回待落库（已 flush）的事件，否则 None。"""
    members = {x for x in (room.members or "").split(",") if x}

    msgs = (
        db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.room_id == room.room_id,
                ChatMessage.action == "send",
                ChatMessage.msg_type != "revoke",
            )
            .order_by(ChatMessage.msg_time)
        )
        .scalars()
        .all()
    )

    has_emp = False
    first_customer = None  # 当前"未回复客户消息段"的第一条
    for m in msgs:
        if _is_employee(m.from_id, members):
            has_emp = True
            # 员工回复 → 重置未回复段
            first_customer = None
        else:
            if first_customer is None:
                first_customer = m

    if first_customer is None or not has_emp:
        return None

    elapsed = (now - first_customer.msg_time).total_seconds() / 60 if first_customer.msg_time else 0
    if elapsed < minutes:
        return None

    # 去重：同一首条客户消息 + 同一分类只建一次
    exists = db.execute(
        select(RiskEvent.id).where(
            RiskEvent.message_id == first_customer.id,
            RiskEvent.category == cat.CATEGORY_REPLY_TIMEOUT,
        )
    ).scalar_one_or_none()
    if exists:
        return None

    biz = first_customer.msg_time
    snippet = (
        f"客户消息超过 {minutes} 分钟未获回复"
        + (f"（首条于 {biz:%Y-%m-%d %H:%M}）" if biz else "")
    )
    ev = RiskEvent(
        message_id=first_customer.id,
        room_id=room.room_id,
        from_id=first_customer.from_id,
        rule_id=None,
        category=cat.CATEGORY_REPLY_TIMEOUT,
        severity=severity,
        detection_method="timeout",
        matched_keyword=None,
        snippet=snippet,
        detail=(
            f"群 {room.room_id} 自 {biz:%Y-%m-%d %H:%M} 起连续客户消息未获员工回复，"
            f"已等待约 {int(elapsed)} 分钟（阈值 {minutes} 分钟）。"
        ),
        biz_time=biz,
        alert_status="unsent",
    )
    db.add(ev)
    db.flush()

    layers = list(cat.DEFAULT_SEVERITY_LAYERS.get(severity, ["L1"]))
    try:
        ev.alert_status = sender.dispatch_alert(db, ev, layers)
    except Exception as e:  # noqa: BLE001
        logger.warning("超时预警投递异常 event=%s：%s", ev.id, e)
        ev.alert_status = "failed"
    return ev


def scan_reply_timeouts(db: Session, limit: int | None = None) -> dict:
    """扫描所有群，产出超时回复风险事件。返回统计。"""
    enabled, minutes, severity = _load_cfg(db)
    stats = {
        "enabled": enabled,
        "minutes": minutes,
        "severity": severity,
        "checked_rooms": 0,
        "events": 0,
        "errors": [],
    }
    if not enabled:
        return stats

    rooms = (
        db.execute(
            select(ChatRoom).where(
                ChatRoom.room_id != "", ChatRoom.enabled == True  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now()
    for room in rooms:
        try:
            ev = _scan_room(db, room, minutes, severity, now)
            stats["checked_rooms"] += 1
            if ev is not None:
                stats["events"] += 1
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(str(e)[:200])
            logger.exception("超时扫描群异常 room=%s：%s", room.room_id, e)
    return stats
