"""
app/api/messages.py — 消息与群档案查询接口
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.api.schemas import (
    MessageDetail,
    MessageOut,
    Page,
    RoomOut,
    RoomUpdate,
)
from app.db.database import get_db
from app.models.entities import ChatMessage, ChatRoom
from app.models.risk import RiskEvent
from app.services.auth.rbac import require_perm

router = APIRouter()


@router.get("/messages", response_model=Page[MessageOut], summary="消息列表", dependencies=[Depends(require_perm("messages", "view"))])
def list_messages(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    room_id: str | None = Query(None, description="按群过滤"),
    msg_type: str | None = Query(None, description="text/image/file/..."),
    from_id: str | None = Query(None),
    keyword: str | None = Query(None, description="正文模糊搜索"),
    has_attachment: bool | None = Query(None),
    start: datetime | None = Query(None, description="消息时间起"),
    end: datetime | None = Query(None, description="消息时间止"),
):
    conds = []
    if room_id:
        conds.append(ChatMessage.room_id == room_id)
    if msg_type:
        conds.append(ChatMessage.msg_type == msg_type)
    if from_id:
        conds.append(ChatMessage.from_id == from_id)
    if keyword:
        conds.append(ChatMessage.content_text.ilike(f"%{keyword}%"))
    if has_attachment is True:
        conds.append(ChatMessage.attachment_count > 0)
    elif has_attachment is False:
        conds.append(ChatMessage.attachment_count == 0)
    if start:
        conds.append(ChatMessage.msg_time >= start)
    if end:
        conds.append(ChatMessage.msg_time <= end)

    total = db.execute(
        select(func.count(ChatMessage.id)).where(*conds)
    ).scalar_one()

    rows = (
        db.execute(
            select(ChatMessage)
            .where(*conds)
            .order_by(ChatMessage.seq.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return Page[MessageOut](
        total=total, page=page, page_size=page_size,
        items=[MessageOut.model_validate(r) for r in rows],
    )


@router.get("/messages/{message_id}", response_model=MessageDetail, summary="消息详情", dependencies=[Depends(require_perm("messages", "view"))])
def get_message(message_id: str, db: Session = Depends(get_db)):
    msg = db.execute(
        select(ChatMessage)
        .options(selectinload(ChatMessage.attachments))
        .where(ChatMessage.id == message_id)
    ).scalar_one_or_none()
    if msg is None:
        raise HTTPException(404, "消息不存在")
    return MessageDetail.model_validate(msg)


@router.get("/rooms", response_model=list[RoomOut], summary="群列表", dependencies=[Depends(require_perm("rooms", "view"))])
def list_rooms(db: Session = Depends(get_db)):
    rows = (
        db.execute(select(ChatRoom).order_by(ChatRoom.last_msg_at.desc().nullslast()))
        .scalars()
        .all()
    )
    return [RoomOut.model_validate(r) for r in rows]


@router.patch("/rooms/{room_id}", response_model=RoomOut, summary="更新群备注/开关", dependencies=[Depends(require_perm("rooms", "edit"))])
def update_room(room_id: str, payload: RoomUpdate, db: Session = Depends(get_db)):
    room = db.get(ChatRoom, room_id)
    if room is None:
        raise HTTPException(404, "群不存在")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(room, k, v)
    db.commit()
    db.refresh(room)
    return RoomOut.model_validate(room)


@router.delete("/messages/{message_id}", summary="删除一条消息（连带附件，风险事件保留但解除关联）", dependencies=[Depends(require_perm("messages", "delete"))])
def delete_message(message_id: str, db: Session = Depends(get_db)):
    """删除单条消息：其附件随外键级联删除；关联的风险事件保留（message_id 置空，仍可按群追溯）。"""
    msg = db.get(ChatMessage, message_id)
    if msg is None:
        raise HTTPException(404, "消息不存在")
    # 风险事件解除与本消息的关联（message_id 为 SET NULL 约束）
    db.execute(sa_delete(RiskEvent).where(RiskEvent.message_id == message_id))
    db.delete(msg)
    db.commit()
    return {"deleted": message_id, "ok": True}
