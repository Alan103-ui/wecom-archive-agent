"""
scripts/seed_demo_records.py — 把真实单据测试结果种进前端可展示的数据链路

模拟一个群 + 一个联系人，将 data/real_docs_test/rows.jsonl 的 5 张真实送货单
串成完整链路：ChatMessage(联系人发图) → Attachment(图复制进 MEDIA_ROOT)
→ OcrResult → ExtractedRecord(模板=送货单)，全部挂到该群 + 联系人。

这样前端「结构化数据」「群消息」都能看到这 5 张单的真实抽取效果与原图。
幂等：检测到演示群已存在则跳过，避免重复种数据。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(r"D:/Clow/projects/wecom-archive-agent")
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.database import SessionLocal  # noqa: E402
from app.models.entities import (  # noqa: E402
    Attachment,
    ChatMessage,
    ChatRoom,
    ExternalContact,
    ExtractTemplate,
    ExtractedRecord,
    OcrResult,
)

SRC = ROOT / "data" / "real_docs_test"
MEDIA = ROOT / "data" / "media" / "demo"
MEDIA.mkdir(parents=True, exist_ok=True)

ROOM_ID = "wrkDemoDeliv001"
ROOM_NAME = "【演示】送货单识别测试群"
CONTACT_ID = "wmDemoSupplier01"
CONTACT_NAME = "演示供应商·王经理"
CORP_NAME = "演示科技有限公司"


def _parse_biz(dd: str | None) -> datetime:
    if dd:
        try:
            return datetime.strptime(dd, "%Y-%m-%d")
        except Exception:
            pass
    return datetime(2026, 8, 1)


def main() -> None:
    db = SessionLocal()
    try:
        if db.get(ChatRoom, ROOM_ID) is not None:
            print("演示数据已存在（room_id=%s），跳过" % ROOM_ID)
            return

        tpl = db.execute(
            select(ExtractTemplate).where(ExtractTemplate.name == "送货单")
        ).scalar_one_or_none()
        if tpl is None:
            raise SystemExit("找不到模板「送货单」，请先初始化模板")
        tpl_id = tpl.id

        rows = [json.loads(l) for l in open(SRC / "rows.jsonl", encoding="utf-8") if l.strip()]
        if not rows:
            raise SystemExit("rows.jsonl 为空")

        # 1) 模拟群 + 模拟联系人
        room = ChatRoom(
            room_id=ROOM_ID,
            name=ROOM_NAME,
            owner=CONTACT_ID,
            member_count=2,
            members=CONTACT_ID + ",",
            msg_count=len(rows),
            attachment_count=len(rows),
            last_msg_at=None,
            enabled=True,
        )
        db.add(room)
        db.add(
            ExternalContact(
                external_userid=CONTACT_ID,
                name=CONTACT_NAME,
                corp_name=CORP_NAME,
                type=1,
                gender=1,
            )
        )

        seq = 9_900_000
        last_msg_at = None
        for i, row in enumerate(rows, start=1):
            img = row["img"]
            src = SRC / img
            if not src.exists():
                print("  跳过缺失文件:", img)
                continue
            dst = MEDIA / f"demo_{img}"
            shutil.copy(src, dst)

            fields = row.get("final_fields") or row.get("ocr_fields") or {}
            conf = float(row.get("ocr_extract_conf") or 0.6)
            model = row.get("final_model") or row.get("ocr_model") or "zhipu-glm-5-turbo"
            dur = int(row.get("duration_ms") or 0)
            text = row.get("ocr_text_preview") or ""
            ocr_conf = float(row.get("ocr_conf") or 0)
            method = row.get("method") or "ocr"
            warnings = row.get("final_warnings") or row.get("ocr_warnings") or []
            biz = _parse_biz(fields.get("delivery_date"))
            if last_msg_at is None or biz > last_msg_at:
                last_msg_at = biz
            mtime = int(biz.timestamp() * 1000)

            msg_id = uuid4().hex
            att_id = uuid4().hex
            rec_id = uuid4().hex
            msgid = f"demo-{i}-{uuid4().hex[:8]}"
            ext = Path(img).suffix.lower().lstrip(".")

            db.add(
                ChatMessage(
                    id=msg_id,
                    seq=seq,
                    msgid=msgid,
                    action="send",
                    msg_type="image",
                    from_id=CONTACT_ID,
                    from_name=CONTACT_NAME,
                    to_list=None,
                    room_id=ROOM_ID,
                    msg_time_ms=mtime,
                    msg_time=biz,
                    content_text=f"[图片] {img}",
                    raw_json=None,
                    attachment_count=1,
                    risk_scanned=True,
                )
            )
            db.add(
                Attachment(
                    id=att_id,
                    message_id=msg_id,
                    room_id=ROOM_ID,
                    msgid=msgid,
                    media_type="image",
                    sdkfileid="demo",
                    file_name=img,
                    file_ext=ext,
                    file_size=src.stat().st_size,
                    md5sum=None,
                    local_path=str(dst),
                    download_status="done",
                    download_error=None,
                    download_retry=0,
                    downloaded_at=biz,
                    ocr_status="done",
                    extract_status="done",
                )
            )
            db.add(
                OcrResult(
                    id=uuid4().hex,
                    attachment_id=att_id,
                    engine="rapidocr",
                    status="done",
                    text_content=text,
                    blocks_json=None,
                    page_count=1,
                    text_length=len(text),
                    avg_confidence=ocr_conf,
                    duration_ms=0,
                    error=None,
                )
            )
            db.add(
                ExtractedRecord(
                    id=rec_id,
                    message_id=msg_id,
                    attachment_id=att_id,
                    room_id=ROOM_ID,
                    msgid=msgid,
                    template_id=tpl_id,
                    template_name="送货单",
                    status="done",
                    fields_json=fields,
                    confidence=conf,
                    model=model,
                    extract_method=method,
                    extract_warnings=warnings,
                    duration_ms=dur,
                    error=None,
                    reviewed=False,
                    biz_time=biz,
                    created_at=datetime.now(),
                )
            )
            seq += 1
            print(f"  已种入 {img} → 记录 {rec_id[:8]}")

        if last_msg_at:
            room.last_msg_at = last_msg_at
        db.commit()
        print(f"OK：{len(rows)} 张真实单据已挂到群「{ROOM_NAME}」({ROOM_ID})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
