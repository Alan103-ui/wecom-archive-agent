"""
app/api/rooms.py — 群（房间）管理

提供群的列表 / 详情 / 采集开关 / 批量开关。
采集开关直接驱动 pipeline.sync_messages：关闭后该群新消息不再入库，
历史已采集数据保留。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.entities import ChatRoom, ChatMessage, Attachment, ExtractedRecord
from app.models.risk import RiskEvent
from app.config import settings
from app.services.auth.rbac import require_perm

from pathlib import Path
from sqlalchemy import delete as sa_delete


def _safe_filename(name: str, fallback_ext: str = ".bin") -> str:
    """清洗文件名，防目录穿越与非法字符（与 pipeline._safe_filename 保持一致）"""
    import re
    cleaned = re.sub(r"[^\w.\-]", "_", (name or "").strip()) or f"file{fallback_ext}"
    return cleaned[:120]


router = APIRouter()


class DeleteRoomResult(BaseModel):
    room_id: str
    deleted_messages: int
    deleted_attachments: int
    deleted_risk_events: int
    deleted_records: int
    media_dir_removed: bool


class ChatRoomOut(BaseModel):
    room_id: str
    name: str | None = None
    owner: str | None = None
    member_count: int = 0
    members: list[str] = []
    msg_count: int = 0
    attachment_count: int = 0
    last_msg_at: str | None = None
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_room(cls, r: ChatRoom) -> "ChatRoomOut":
        members = [m for m in (r.members or "").split(",") if m] if r.members else []
        return cls(
            room_id=r.room_id,
            name=r.name,
            owner=r.owner,
            member_count=r.member_count,
            members=members,
            msg_count=r.msg_count,
            attachment_count=r.attachment_count,
            last_msg_at=r.last_msg_at.isoformat() if r.last_msg_at else None,
            enabled=r.enabled,
            created_at=r.created_at.isoformat() if r.created_at else None,
            updated_at=r.updated_at.isoformat() if r.updated_at else None,
        )


class RoomPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = None


class BatchToggle(BaseModel):
    enabled: bool
    room_ids: list[str] | None = None


def _get_or_404(db: Session, room_id: str) -> ChatRoom:
    room = db.get(ChatRoom, room_id)
    if room is None:
        raise HTTPException(404, f"群不存在：{room_id}")
    return room


@router.get("", response_model=list[ChatRoomOut], summary="列出所有群", dependencies=[Depends(require_perm("rooms", "view"))])
def list_rooms(db: Session = Depends(get_db)):
    rows = db.execute(
        select(ChatRoom).order_by(ChatRoom.name, ChatRoom.room_id)
    ).scalars().all()
    return [ChatRoomOut.from_room(r) for r in rows]


@router.get("/{room_id}", response_model=ChatRoomOut, summary="群详情", dependencies=[Depends(require_perm("rooms", "view"))])
def get_room(room_id: str, db: Session = Depends(get_db)):
    return ChatRoomOut.from_room(_get_or_404(db, room_id))


@router.patch("/{room_id}", response_model=ChatRoomOut, summary="更新群（采集开关 / 备注名）", dependencies=[Depends(require_perm("rooms", "edit"))])
def patch_room(room_id: str, body: RoomPatch, db: Session = Depends(get_db)):
    room = _get_or_404(db, room_id)
    if body.enabled is not None:
        room.enabled = body.enabled
    if body.name is not None:
        room.name = body.name or None
    db.commit()
    return ChatRoomOut.from_room(room)


@router.post("/batch-toggle", summary="批量设置采集开关（全部群或指定群）", dependencies=[Depends(require_perm("rooms", "edit"))])
def batch_toggle(body: BatchToggle, db: Session = Depends(get_db)):
    stmt = select(ChatRoom)
    if body.room_ids:
        stmt = stmt.where(ChatRoom.room_id.in_(body.room_ids))
    rooms = db.execute(stmt).scalars().all()
    for r in rooms:
        r.enabled = body.enabled
    db.commit()
    return {"updated": len(rooms), "enabled": body.enabled}


@router.delete("/{room_id}", response_model=DeleteRoomResult, summary="删除群及其全部存档数据", dependencies=[Depends(require_perm("rooms", "delete"))])
def delete_room(room_id: str, db: Session = Depends(get_db)):
    """删除群：连带清除该群所有已存档消息、附件、风险事件（及投递回执）与结构化记录，
    并删除本地媒体目录。操作不可逆，前端需二次确认。
    """
    room = db.get(ChatRoom, room_id)
    if room is None:
        raise HTTPException(404, f"群不存在：{room_id}")

    # 1) 附件（含按 room_id 冗余字段 + 通过 message_id 外键级联）
    n_att = db.execute(sa_delete(Attachment).where(Attachment.room_id == room_id)).rowcount
    # 2) 消息
    n_msg = db.execute(sa_delete(ChatMessage).where(ChatMessage.room_id == room_id)).rowcount
    # 3) 风险事件（relationship 级联删 AlertLog）
    n_risk = db.execute(sa_delete(RiskEvent).where(RiskEvent.room_id == room_id)).rowcount
    # 4) 结构化记录
    n_rec = db.execute(sa_delete(ExtractedRecord).where(ExtractedRecord.room_id == room_id)).rowcount
    # 5) 群本身
    db.delete(room)
    db.commit()

    # 6) 本地媒体目录
    media_dir = Path(settings.MEDIA_ROOT) / (_safe_filename(room_id) or "")
    removed = False
    if media_dir.exists():
        try:
            import shutil
            shutil.rmtree(media_dir, ignore_errors=True)
            removed = True
        except Exception:  # noqa: BLE001
            removed = media_dir.exists() is False

    return DeleteRoomResult(
        room_id=room_id,
        deleted_messages=n_msg,
        deleted_attachments=n_att,
        deleted_risk_events=n_risk,
        deleted_records=n_rec,
        media_dir_removed=removed,
    )
