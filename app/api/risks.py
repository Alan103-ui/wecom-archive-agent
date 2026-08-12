"""
app/api/risks.py — 风险预警接口

覆盖：风险事件（列表/详情/统计/确认/重发预警）、风险规则 CRUD、
管理层与目标 CRUD、回填重扫。

所有路由挂在 /api/risks 下，由 api_router 统一加 /api 前缀。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import ActionResult, Page
from app.config import settings
from app.db.database import get_db
from app.models.entities import ChatMessage
from app.models.risk import AlertLayer, AlertLog, AlertTarget, RiskEvent, RiskRule
from app.services import pipeline
from app.services.alert import sender
from app.services.risk import categories as cat
from app.services.risk.detector import load_rules

router = APIRouter()


# ---------------------------------------------------------------- 出入参
class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    message_id: str | None = None
    room_id: str
    from_id: str | None = None
    rule_id: str | None = None
    category: str
    severity: str
    detection_method: str
    matched_keyword: str | None = None
    snippet: str | None = None
    detail: str | None = None
    status: str
    alert_status: str
    biz_time: datetime | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None
    created_at: datetime | None = None


class RuleCreate(BaseModel):
    name: str
    category: str
    severity: str = "medium"
    description: str | None = None
    scope_rooms: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    llm_prompt: str | None = None
    alert_layers: list[str] = Field(default_factory=list)
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    severity: str | None = None
    scope_rooms: list[str] | None = None
    keywords: list[str] | None = None
    llm_prompt: str | None = None
    alert_layers: list[str] | None = None
    enabled: bool | None = None


class LayerCreate(BaseModel):
    id: str
    name: str
    level: int = 1
    description: str | None = None


class LayerUpdate(BaseModel):
    name: str | None = None
    level: int | None = None
    description: str | None = None


class TargetCreate(BaseModel):
    layer_id: str
    channel: str  # webhook / app / email / system
    target: str = ""
    label: str | None = None
    enabled: bool = True


class TargetUpdate(BaseModel):
    target: str | None = None
    label: str | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------- 事件列表/统计
@router.get("/events", response_model=Page[EventOut], summary="风险事件列表")
def list_events(
    db: Session = Depends(get_db),
    status: str | None = Query(None, description="pending/acknowledged/resolved/ignored"),
    severity: str | None = None,
    category: str | None = None,
    room_id: str | None = None,
    detection_method: str | None = None,
    alert_status: str | None = Query(None, description="逗号分隔，如 failed,partial"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    stmt = select(RiskEvent)
    if status:
        stmt = stmt.where(RiskEvent.status == status)
    if severity:
        stmt = stmt.where(RiskEvent.severity == severity)
    if category:
        stmt = stmt.where(RiskEvent.category == category)
    if room_id:
        stmt = stmt.where(RiskEvent.room_id == room_id)
    if detection_method:
        stmt = stmt.where(RiskEvent.detection_method == detection_method)
    if alert_status:
        vals = [v.strip() for v in alert_status.split(",") if v.strip()]
        if vals:
            stmt = stmt.where(RiskEvent.alert_status.in_(vals))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    items = (
        db.execute(stmt.order_by(RiskEvent.created_at.desc())
                  .limit(page_size).offset((page - 1) * page_size))
        .scalars().all()
    )
    return Page(total=total, page=page, page_size=page_size, items=items)


@router.get("/stats", summary="风险统计看板")
def risk_stats(db: Session = Depends(get_db)):
    def _group(col):
        return {k or "unknown": v for k, v in db.execute(select(col, func.count()).group_by(col)).all()}

    total = db.execute(select(func.count()).select_from(RiskEvent)).scalar_one()
    pending = db.execute(
        select(func.count()).select_from(RiskEvent).where(RiskEvent.status == "pending")
    ).scalar_one()
    # 按日（近 14 天）
    from sqlalchemy import text as _text

    daily = db.execute(_text(
        "SELECT substr(created_at,1,10) d, count(*) c FROM risk_event "
        "GROUP BY d ORDER BY d DESC LIMIT 14"
    )).all()
    return {
        "total": total,
        "pending": pending,
        "by_severity": _group(RiskEvent.severity),
        "by_category": _group(RiskEvent.category),
        "by_status": _group(RiskEvent.status),
        "by_alert_status": _group(RiskEvent.alert_status),
        "by_room": _group(RiskEvent.room_id),
        "daily": [{"date": d, "count": c} for d, c in daily],
    }


@router.get("/events/{event_id}", response_model=EventOut, summary="事件详情")
def get_event(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(RiskEvent, event_id)
    if ev is None:
        raise HTTPException(404, "事件不存在")
    return ev


@router.get("/events/{event_id}/logs", summary="事件投递回执")
def event_logs(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(RiskEvent, event_id)
    if ev is None:
        raise HTTPException(404, "事件不存在")
    logs = db.execute(
        select(AlertLog).where(AlertLog.event_id == event_id).order_by(AlertLog.sent_at)
    ).scalars().all()
    return [
        {"id": l.id, "layer_id": l.layer_id, "channel": l.channel, "target": l.target,
         "status": l.status, "detail": l.detail, "sent_at": l.sent_at.isoformat() if l.sent_at else None}
        for l in logs
    ]


@router.get("/logs", summary="全局投递回执（全链条送达结果）")
def list_logs(
    db: Session = Depends(get_db),
    status: str | None = Query(None, description="sent/failed"),
    channel: str | None = None,
    layer_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
):
    """投递回执总览：按状态/通道/层过滤，关联事件取分类/严重度/群。

    这是「规则 → 管理层 → 投递目标 → 实际送达」链条的最后一环可视化入口：
    每一行是一次真实投递（或失败）的回执，可据此核验告警是否真的到达。
    """
    from sqlalchemy import and_ as _and

    conds = []
    if status:
        conds.append(AlertLog.status == status)
    if channel:
        conds.append(AlertLog.channel == channel)
    if layer_id:
        conds.append(AlertLog.layer_id == layer_id)
    w = _and(*conds) if conds else True

    total = db.execute(select(func.count()).select_from(AlertLog).where(w)).scalar_one()
    by_status = {
        k: v for k, v in db.execute(
            select(AlertLog.status, func.count()).select_from(AlertLog).where(w).group_by(AlertLog.status)
        ).all()
    }
    by_channel = {
        k: v for k, v in db.execute(
            select(AlertLog.channel, func.count()).select_from(AlertLog).where(w).group_by(AlertLog.channel)
        ).all()
    }
    rows = (
        db.execute(
            select(AlertLog, RiskEvent)
            .join(RiskEvent, AlertLog.event_id == RiskEvent.id, isouter=True)
            .where(w)
            .order_by(AlertLog.sent_at.desc())
            .limit(page_size).offset((page - 1) * page_size)
        )
        .all()
    )
    items = []
    for l, ev in rows:
        items.append({
            "id": l.id,
            "event_id": l.event_id,
            "layer_id": l.layer_id,
            "channel": l.channel,
            "target": l.target,
            "status": l.status,
            "detail": l.detail,
            "sent_at": l.sent_at.isoformat() if l.sent_at else None,
            "category": ev.category if ev else None,
            "severity": ev.severity if ev else None,
            "room_id": ev.room_id if ev else None,
            "snippet": (ev.snippet or "")[:60] if ev else None,
        })
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "by_status": by_status,
        "by_channel": by_channel,
        "items": items,
    }


@router.post("/events/{event_id}/acknowledge", response_model=ActionResult, summary="确认/处置事件")
def acknowledge(event_id: str, reviewer: str = Body("system", embed=True),
                note: str | None = Body(None, embed=True)):
    db = next(get_db())
    ev = db.get(RiskEvent, event_id)
    if ev is None:
        raise HTTPException(404, "事件不存在")
    ev.status = "acknowledged"
    ev.acknowledged_by = reviewer
    ev.acknowledged_at = __import__("datetime").datetime.now()
    if note:
        ev.detail = (ev.detail or "") + f"\n[处置]{note}"
    db.commit()
    return ActionResult(message="已确认", data={"id": event_id})


@router.post("/events/{event_id}/resend", response_model=ActionResult, summary="重新发送预警")
def resend(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(RiskEvent, event_id)
    if ev is None:
        raise HTTPException(404, "事件不存在")
    # 路由：优先用规则显式层，否则按 severity 兜底
    layers = []
    if ev.rule_id:
        rule = db.get(RiskRule, ev.rule_id)
        if rule and rule.alert_layers:
            layers = list(rule.alert_layers)
    if not layers:
        layers = list(cat.DEFAULT_SEVERITY_LAYERS.get(ev.severity, ["L1"]))
    try:
        status = sender.dispatch_alert(db, ev, layers)
        ev.alert_status = status
        db.commit()
        return ActionResult(message="已重发", data={"alert_status": status})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"重发失败：{e}")


@router.post("/rescan", response_model=ActionResult, summary="回填/重扫全部消息")
def rescan(room_id: str | None = Body(None, embed=True), limit: int | None = Body(None, embed=True)):
    db = next(get_db())
    n = pipeline.risk_rescan(db, room_id=room_id, limit=limit)
    return ActionResult(message=f"已重置 {n} 条消息为待扫描，下一轮风险作业将重扫", data={"count": n})


@router.post("/timeout-scan", response_model=ActionResult, summary="立即执行超时回复扫描")
def timeout_scan():
    """手动触发一次超时回复提醒扫描（调度作业之外，便于即时验证/补扫）。"""
    stats = pipeline.reply_timeout_scan()
    return ActionResult(message="超时回复扫描完成", data=stats)


# ---------------------------------------------------------------- 规则
@router.get("/rules", summary="风险规则列表")
def list_rules(db: Session = Depends(get_db)):
    return db.execute(
        select(RiskRule).order_by(RiskRule.priority.desc(), RiskRule.created_at)
    ).scalars().all()


@router.post("/rules", response_model=ActionResult, summary="新建规则")
def create_rule(body: RuleCreate, db: Session = Depends(get_db)):
    if body.category not in cat.ALL_CATEGORIES:
        raise HTTPException(400, f"未知分类：{body.category}")
    if body.severity not in cat.SEVERITY_ORDER:
        raise HTTPException(400, f"未知严重度：{body.severity}")
    rule = RiskRule(
        name=body.name, description=body.description, category=body.category,
        severity=body.severity, scope_rooms=body.scope_rooms, keywords=body.keywords,
        llm_prompt=body.llm_prompt, alert_layers=body.alert_layers, enabled=body.enabled,
    )
    db.add(rule)
    db.commit()
    return ActionResult(message="已创建规则", data={"id": rule.id})


@router.patch("/rules/{rule_id}", response_model=ActionResult, summary="更新规则")
def update_rule(rule_id: str, body: RuleUpdate, db: Session = Depends(get_db)):
    rule = db.get(RiskRule, rule_id)
    if rule is None:
        raise HTTPException(404, "规则不存在")
    for f in ("name", "description", "category", "severity", "scope_rooms",
              "keywords", "llm_prompt", "alert_layers", "enabled"):
        v = getattr(body, f)
        if v is not None:
            setattr(rule, f, v)
    rule.updated_at = __import__("datetime").datetime.now()
    db.commit()
    return ActionResult(message="已更新", data={"id": rule_id})


@router.delete("/rules/{rule_id}", response_model=ActionResult, summary="删除规则")
def delete_rule(rule_id: str, db: Session = Depends(get_db)):
    rule = db.get(RiskRule, rule_id)
    if rule is None:
        raise HTTPException(404, "规则不存在")
    db.delete(rule)
    db.commit()
    return ActionResult(message="已删除")


# ---------------------------------------------------------------- 管理层与投递目标
@router.get("/layers", summary="管理层与投递目标")
def list_layers(db: Session = Depends(get_db)):
    layers = db.execute(select(AlertLayer).order_by(AlertLayer.level)).scalars().all()
    out = []
    for l in layers:
        targets = db.execute(
            select(AlertTarget).where(AlertTarget.layer_id == l.id)
        ).scalars().all()
        out.append({
            "id": l.id, "name": l.name, "level": l.level, "description": l.description,
            "targets": [{
                "id": t.id, "channel": t.channel, "target": t.target,
                "label": t.label, "enabled": t.enabled,
            } for t in targets],
        })
    return out


@router.post("/layers", response_model=ActionResult, summary="新建管理层")
def create_layer(body: LayerCreate, db: Session = Depends(get_db)):
    exists = db.get(AlertLayer, body.id)
    if exists:
        raise HTTPException(400, "该 id 已存在")
    db.add(AlertLayer(id=body.id, name=body.name, level=body.level, description=body.description))
    db.commit()
    return ActionResult(message="已创建管理层", data={"id": body.id})


@router.patch("/layers/{layer_id}", response_model=ActionResult, summary="更新管理层")
def update_layer(layer_id: str, body: LayerUpdate, db: Session = Depends(get_db)):
    l = db.get(AlertLayer, layer_id)
    if l is None:
        raise HTTPException(404, "管理层不存在")
    for f in ("name", "level", "description"):
        v = getattr(body, f)
        if v is not None:
            setattr(l, f, v)
    db.commit()
    return ActionResult(message="已更新")


@router.delete("/layers/{layer_id}", response_model=ActionResult, summary="删除管理层")
def delete_layer(layer_id: str, db: Session = Depends(get_db)):
    l = db.get(AlertLayer, layer_id)
    if l is None:
        raise HTTPException(404, "管理层不存在")
    db.delete(l)
    db.commit()
    return ActionResult(message="已删除（其投递目标一并删除）")


@router.post("/targets", response_model=ActionResult, summary="新建投递目标")
def create_target(body: TargetCreate, db: Session = Depends(get_db)):
    if body.channel not in ("webhook", "app", "email", "system"):
        raise HTTPException(400, "通道必须是 webhook/app/email/system")
    if not db.get(AlertLayer, body.layer_id):
        raise HTTPException(400, "layer_id 不存在")
    t = AlertTarget(layer_id=body.layer_id, channel=body.channel, target=body.target,
                    label=body.label, enabled=body.enabled)
    db.add(t)
    db.commit()
    return ActionResult(message="已创建投递目标", data={"id": t.id})


@router.patch("/targets/{target_id}", response_model=ActionResult, summary="更新投递目标")
def update_target(target_id: str, body: TargetUpdate, db: Session = Depends(get_db)):
    t = db.get(AlertTarget, target_id)
    if t is None:
        raise HTTPException(404, "目标不存在")
    if body.target is not None:
        t.target = body.target
    if body.label is not None:
        t.label = body.label
    if body.enabled is not None:
        t.enabled = body.enabled
    db.commit()
    return ActionResult(message="已更新")


@router.delete("/targets/{target_id}", response_model=ActionResult, summary="删除投递目标")
def delete_target(target_id: str, db: Session = Depends(get_db)):
    t = db.get(AlertTarget, target_id)
    if t is None:
        raise HTTPException(404, "目标不存在")
    db.delete(t)
    db.commit()
    return ActionResult(message="已删除")


@router.post("/layers/{layer_id}/test", response_model=ActionResult, summary="测试该层投递")
def test_layer(layer_id: str, db: Session = Depends(get_db)):
    """用一条虚拟事件测试该层所有启用目标的连通性"""
    l = db.get(AlertLayer, layer_id)
    if l is None:
        raise HTTPException(404, "管理层不存在")
    targets = db.execute(
        select(AlertTarget).where(AlertTarget.layer_id == layer_id, AlertTarget.enabled == True)  # noqa: E712
    ).scalars().all()

    class _Fake:
        room_id = "TEST-ROOM"
        from_id = "TEST-USER"
        snippet = "这是一条测试预警消息"
        detail = "连通性测试"
        biz_time = __import__("datetime").datetime.now()
        category = "合规风险"
        severity = "high"

    results = []
    for t in targets:
        ok, detail = sender._send_one(t, _Fake())
        results.append({"channel": t.channel, "target": t.target, "ok": ok, "detail": detail})
    return ActionResult(message="测试完成", data={"results": results})
