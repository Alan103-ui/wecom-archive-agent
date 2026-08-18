"""
tests/test_compliance.py — 敏感信息脱敏 + 留存期清理
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.compliance import mask_if_enabled, mask_sensitive, purge_expired


def test_mask_phone_id_email():
    assert mask_sensitive("联系 13812345678 确认") == "联系 138****5678 确认"
    assert mask_sensitive("身份证 110101199003071234") == "身份证 1101**********1234"
    assert mask_sensitive("邮箱 abc@example.com") == "邮箱 a***@example.com"
    assert mask_sensitive("") == ""
    assert mask_sensitive(None) is None


def test_mask_bank_and_tel():
    assert mask_sensitive("卡号 6222021234567890123") == "卡号 6222********0123"
    assert mask_sensitive("电话 021-58765566") == "电话 021-58****66"


def test_mask_disabled_by_default():
    # DATA_MASK_ENABLED 默认 False：原样返回
    assert mask_if_enabled("13812345678") == "13812345678"


def test_purge_expired(monkeypatch):
    from sqlalchemy import delete, select

    from app.db.database import SessionLocal
    from app.models.entities import ChatMessage, ChatRoom
    from app.models.risk import RiskEvent

    monkeypatch.setattr(settings, "DATA_RETENTION_DAYS", 30)
    rid = "purge_room_x"
    db = SessionLocal()
    try:
        db.add(ChatRoom(room_id=rid, name="清理测试群"))
        old = ChatMessage(
            id="purge_old", seq=1, msgid="purge_old", msg_type="text",
            room_id=rid, content_text="old",
            created_at=datetime.now() - timedelta(days=40),
        )
        new = ChatMessage(
            id="purge_new", seq=2, msgid="purge_new", msg_type="text",
            room_id=rid, content_text="new",
        )
        db.add_all([old, new])
        db.add(RiskEvent(id="purge_ev", category="test", message_id="purge_old", room_id=rid))
        db.commit()

        res = purge_expired(db)
        assert res["deleted_messages"] == 1, res
        assert res["deleted_events"] == 1, res
        # 新消息保留
        left = db.execute(select(ChatMessage).where(ChatMessage.id == "purge_new")).scalar_one_or_none()
        assert left is not None
    finally:
        db.execute(delete(ChatMessage).where(ChatMessage.room_id == rid))
        db.execute(delete(RiskEvent).where(RiskEvent.id == "purge_ev"))
        db.execute(delete(ChatRoom).where(ChatRoom.room_id == rid))
        db.commit()
        db.close()
