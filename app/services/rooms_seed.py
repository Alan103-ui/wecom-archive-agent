"""
app/services/rooms_seed.py — mock 模式下播种演示群（带备注名）

archive（真实会话存档）模式下，群由同步自动建档（企业微信返回群名），
无需播种。mock 模式为避免前端只看到一串 roomid，这里把演示群名固化为可读备注。
"""
from __future__ import annotations

from sqlalchemy import select

from app.collectors.mock import MockCollector
from app.config import settings
from app.models.entities import ChatRoom


def seed_default_rooms(db) -> None:
    """仅 mock 模式：确保演示群存在且带备注名。"""
    if settings.COLLECTOR_MODE != "mock":
        return
    for room_id, name in MockCollector._ROOMS:
        room = db.get(ChatRoom, room_id)
        if room is None:
            db.add(ChatRoom(room_id=room_id, name=name, enabled=True))
        elif not room.name:
            room.name = name
    db.commit()
