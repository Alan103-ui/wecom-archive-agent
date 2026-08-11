"""
tests/test_timeout.py — 超时回复提醒核心逻辑单测

不依赖运行中的服务/数据库：用独立内存 SQLite 建表，构造群消息时间线，
直接驱动 app.services.risk.timeout._scan_room 验证：
  1. 客户消息超时无员工回复 → 生成「服务响应超时」事件
  2. 客户消息后有员工回复 → 不生成事件
  3. 重复扫描去重（同 首条客户消息+分类 不重复建）
  4. 员工/客户判定：成员列表优先；否则按 wo/wm 前缀兜底
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.entities import ChatMessage, ChatRoom
from app.models.risk import RiskEvent
from app.services.risk import categories as cat
from app.services.risk import timeout


def _make_session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng, expire_on_commit=False)()


def _msg(room, from_id, minutes_ago, seq=1, action="send", mtype="text"):
    return ChatMessage(
        seq=seq, msgid=f"m_{room}_{from_id}_{minutes_ago}",
        action=action, msg_type=mtype, from_id=from_id, room_id=room,
        msg_time_ms=0, msg_time=datetime.now() - timedelta(minutes=minutes_ago),
        content_text="x", raw_json={},
    )


def test_unanswered_customer_burst_triggers():
    db = _make_session()
    room = ChatRoom(room_id="R1", name="售后群", members="", enabled=True)
    db.add(room)
    # 员工曾问候(久) → has_emp=True；客户连发(50/48 分钟前)无人回复
    db.add(_msg("R1", "user_kefu", 180))
    db.add(_msg("R1", "woCust001", 50))
    db.add(_msg("R1", "woCust001", 48))
    db.commit()

    now = datetime.now()
    ev = timeout._scan_room(db, room, minutes=30, severity="medium", now=now)
    assert ev is not None, "应生成超时事件"
    assert ev.category == cat.CATEGORY_REPLY_TIMEOUT
    assert ev.severity == "medium"
    assert ev.detection_method == "timeout"
    assert ev.from_id == "woCust001"
    db.close()


def test_customer_replied_by_employee_no_event():
    db = _make_session()
    room = ChatRoom(room_id="R2", name="群2", members="", enabled=True)
    db.add(room)
    db.add(_msg("R2", "woCust001", 50))
    db.add(_msg("R2", "user_kefu", 10))  # 员工 10 分钟前回复 → 段被回答
    db.commit()

    ev = timeout._scan_room(db, room, minutes=30, severity="medium", now=datetime.now())
    assert ev is None, "员工已回复，不应生成超时事件"
    db.close()


def test_dedup_same_first_customer():
    db = _make_session()
    room = ChatRoom(room_id="R3", name="群3", members="", enabled=True)
    db.add(room)
    db.add(_msg("R3", "user_kefu", 180))
    db.add(_msg("R3", "woCust001", 50))
    db.commit()
    now = datetime.now()
    ev1 = timeout._scan_room(db, room, minutes=30, severity="medium", now=now)
    ev2 = timeout._scan_room(db, room, minutes=30, severity="medium", now=now)
    assert ev1 is not None and ev2 is None, "重复扫描应去重"
    assert db.query(RiskEvent).count() == 1
    db.close()


def test_member_list_priority_over_prefix():
    db = _make_session()
    # 成员列表含 woCust001 → 即便前缀像外部客户，也视为员工
    room = ChatRoom(room_id="R4", name="群4", members="woCust001", enabled=True)
    db.add(room)
    db.add(_msg("R4", "woCust001", 50))  # 在成员列表内 → 员工
    db.commit()
    ev = timeout._scan_room(db, room, minutes=30, severity="medium", now=datetime.now())
    assert ev is None, "成员列表优先：woCust001 视为员工，无客户消息，不触发"
    db.close()


def test_no_employee_in_room_skipped():
    db = _make_session()
    room = ChatRoom(room_id="R5", name="纯客户群", members="", enabled=True)
    db.add(room)
    db.add(_msg("R5", "woCust001", 50))  # 只有客户，没有员工
    db.commit()
    ev = timeout._scan_room(db, room, minutes=30, severity="medium", now=datetime.now())
    assert ev is None, "无员工消息的群不应误报"
    db.close()
