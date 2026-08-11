"""干净环境全量重建 + 采集开关验证（无并发）。"""
import sys, os
sys.path.insert(0, r"d:/Clow/projects/wecom-archive-agent")

from sqlalchemy import select, func, delete, text
from app.db.database import SessionLocal, init_db
from app.models.entities import ChatMessage, Attachment, ChatRoom, ExtractedRecord, SyncCursor
from app.models.risk import RiskEvent
from app.services import pipeline
from app.services.rooms_seed import seed_default_rooms
import app.scheduler as scheduler_mod

ROOM_A = "wrOgQhDgAAv0k1234567890abcdefg"   # 生产运营群
ROOM_B = "wrOgQhDgAAv0k0987654321zyxwvu"    # 供应链协同群
ROOM_C = "wrOgQhDgAAcust0abcdefghijklmn"    # 客户沟通群


def cnt(db, model, room):
    return db.execute(
        select(func.count()).select_from(model).where(model.room_id == room)
    ).scalar_one()


def room_report(db, label):
    print(f"\n--- {label} ---")
    for name, rid in [("A生产", ROOM_A), ("B供应链", ROOM_B), ("C客户", ROOM_C)]:
        m = cnt(db, ChatMessage, rid)
        a = cnt(db, Attachment, rid)
        e = cnt(db, ExtractedRecord, rid)
        r = cnt(db, RiskEvent, rid)
        print(f"  {name}: msg={m} att={a} ext={e} risk={r}")


# 关掉本进程调度（若被启动），避免干扰
try:
    scheduler_mod.shutdown_scheduler()
except Exception:
    pass

db = SessionLocal()
try:
    # 1) 清库重建
    db.execute(text("DELETE FROM chat_message"))
    db.execute(text("DELETE FROM attachment"))
    db.execute(text("DELETE FROM ocr_result"))
    db.execute(text("DELETE FROM extracted_record"))
    db.execute(text("DELETE FROM risk_event"))
    db.execute(text("DELETE FROM chat_room"))
    db.execute(text("DELETE FROM sync_cursor"))
    db.commit()

    # 2) 播种群 + 全量同步
    seed_default_rooms(db)
    st = pipeline.sync_messages(max_rounds=20)
    print("sync stats:", {k: v for k, v in st.items() if k != "errors"})
    if st["errors"]:
        print("SYNC ERRORS:", st["errors"])

    # 3) 阶段二：附件处理 + 风险扫描
    pa = pipeline.process_attachments(batch_size=50)
    print("process_attachments:", pa)
    pipeline.risk_rescan(db)
    rs = pipeline.risk_scan(batch_size=200)
    print("risk_scan:", rs)

    room_report(db, "全量重建后")

    # 4) 验证采集开关：关闭 A，清空 A，重置游标，同步 → A 应为 0
    db.execute(text("UPDATE chat_room SET enabled=0 WHERE room_id=:r"), {"r": ROOM_A})
    db.commit()
    db.execute(delete(RiskEvent).where(RiskEvent.room_id == ROOM_A))
    db.execute(delete(ExtractedRecord).where(ExtractedRecord.room_id == ROOM_A))
    db.execute(delete(Attachment).where(Attachment.room_id == ROOM_A))
    db.execute(delete(ChatMessage).where(ChatMessage.room_id == ROOM_A))
    db.execute(text("UPDATE sync_cursor SET seq=0 WHERE name='default'"))
    db.commit()
    st2 = pipeline.sync_messages(max_rounds=20)
    print("\n关闭A同步 saved:", st2["saved"], "errors:", st2["errors"])
    a_after_close = cnt(db, ChatMessage, ROOM_A)
    b_after_close = cnt(db, ChatMessage, ROOM_B)
    c_after_close = cnt(db, ChatMessage, ROOM_C)
    print(f"关闭A后: A={a_after_close} B={b_after_close} C={c_after_close}")
    assert a_after_close == 0, f"FAIL: 关闭采集仍被采集 A={a_after_close}"
    assert b_after_close > 0 and c_after_close > 0, "FAIL: 其它群未被采集"

    # 5) 重新开启 A，重置游标，同步 → A 应完整恢复
    db.execute(text("UPDATE chat_room SET enabled=1 WHERE room_id=:r"), {"r": ROOM_A})
    db.execute(text("UPDATE sync_cursor SET seq=0 WHERE name='default'"))
    db.commit()
    st3 = pipeline.sync_messages(max_rounds=20)
    print("重新开启A同步 saved:", st3["saved"], "errors:", st3["errors"])
    # 补处理 A 的附件 + 风险
    pipeline.process_attachments(batch_size=50)
    pipeline.risk_rescan(db, room_id=ROOM_A)
    pipeline.risk_scan(batch_size=200)
    db.commit()
    room_report(db, "重新开启A并补全附件/风险后")

    a_final = cnt(db, ChatMessage, ROOM_A)
    a_att = cnt(db, Attachment, ROOM_A)
    a_risk = cnt(db, RiskEvent, ROOM_A)
    print(f"\nA 最终: msg={a_final} att={a_att} risk={a_risk}")
    assert a_final == 4, f"FAIL: A 应恢复 4 条，实际 {a_final}"
    # A 含 seq12 图片 → 至少 1 个附件
    assert a_att >= 1, f"FAIL: A 图片附件丢失 att={a_att}"

    print("\n✅ PASS: 关闭采集生效(A=0,B/C正常)；开启后 A 完整恢复 4 条(含图片附件)且风险重建")
finally:
    db.close()
