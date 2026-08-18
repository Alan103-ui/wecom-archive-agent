"""
tests/test_delete_message_risk_retention.py — 回归测试

验证 delete_message 的契约：删除单条消息时，其关联的风险事件必须保留
（message_id 置空，仍可按群追溯），不得被一并删除。

历史 bug：旧实现误用 sa_delete(RiskEvent) 把关联风险事件直接物理删除，
破坏合规追溯。本测试锁定该修复不回归。
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.database import SessionLocal
from app.main import app
from app.models.entities import ChatMessage
from app.models.risk import RiskEvent


def _client():
    return TestClient(app)


def _login(c: TestClient) -> dict:
    r = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_delete_message_keeps_risk_event():
    tag = uuid.uuid4().hex[:12]
    msg_id = f"regmsg-{tag}"
    msgid = f"regmsgid-{tag}"
    room_id = f"regroom-{tag}"
    ev_id = f"regev-{tag}"

    # 直接落库：一条消息 + 一条关联风险事件
    db = SessionLocal()
    try:
        msg = ChatMessage(
            id=msg_id, seq=1, msgid=msgid, msg_type="text",
            room_id=room_id, content_text="合规回归测试消息",
            risk_scanned=True,  # 置已扫，避免后台调度器扫描产生预警投递副作用
        )
        ev = RiskEvent(id=ev_id, message_id=msg_id, room_id=room_id, category="regression_test")
        db.add(msg)
        db.add(ev)
        db.commit()
    finally:
        db.close()

    with _client() as c:
        h = _login(c)
        r = c.delete(f"/api/messages/{msg_id}", headers=h)
        assert r.status_code == 200, r.text

    # 删除后重新核对
    db = SessionLocal()
    try:
        gone_msg = db.get(ChatMessage, msg_id)
        ev = db.get(RiskEvent, ev_id)
        assert gone_msg is None, "消息应被删除"
        assert ev is not None, "关联风险事件不应被删除"
        assert ev.message_id is None, "风险事件的 message_id 应置空以保留追溯"
    finally:
        # 清理测试数据（按 room 级联清理，覆盖调度器可能产生的关联事件）
        db.execute(RiskEvent.__table__.delete().where(RiskEvent.room_id == room_id))
        db.commit()
        db.close()
