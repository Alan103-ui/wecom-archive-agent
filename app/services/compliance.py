"""
app/services/compliance.py — 合规与脱敏基础

1. mask_sensitive：敏感信息打码（手机号/身份证/银行卡/邮箱/座机），
   仅当 DATA_MASK_ENABLED=true 时生效（生产合规场景开启）。
2. purge_expired：按 DATA_RETENTION_DAYS 清理超期消息及其关联数据（留存期合规）。

注意：脱敏用于"展示层输出"，不修改入库原文（证据链完整）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)(\d{4})\d{10}(\d{4})(?!\d)")
_BANK = re.compile(r"(?<!\d)(\d{4})\d{7,11}(\d{4})(?!\d)")
_TEL = re.compile(r"(?<!\d)(0\d{2,3}-?\d{2})\d{2,4}(\d{2})(?!\d)")
_EMAIL = re.compile(r"(\w)(\w*)(@[\w.-]+)")


def mask_sensitive(text: str | None) -> str | None:
    """对文本中的手机号/身份证/银行卡/座机/邮箱打码（保留首尾，中间 ****）。"""
    if not text:
        return text
    t = _PHONE.sub(r"\1****\2", text)
    t = _ID_CARD.sub(r"\1**********\2", t)
    t = _BANK.sub(r"\1********\2", t)
    t = _TEL.sub(r"\1****\2", t)
    t = _EMAIL.sub(r"\1***\3", t)
    return t


def mask_if_enabled(text: str | None) -> str | None:
    """展示层统一入口：未启用脱敏则原样返回。"""
    if not settings.DATA_MASK_ENABLED:
        return text
    return mask_sensitive(text)


def purge_expired(db: Session) -> dict:
    """按 DATA_RETENTION_DAYS 清理超期数据。0 = 不清理。

    删除范围：超期 ChatMessage + 其关联 Attachment / OcrResult / ExtractedRecord / RiskEvent。
    返回 {deleted_messages, deleted_attachments, deleted_records, deleted_events}
    """
    days = settings.DATA_RETENTION_DAYS
    if not days or days <= 0:
        return {"deleted_messages": 0, "deleted_attachments": 0, "deleted_records": 0, "deleted_events": 0}

    from app.models.entities import Attachment, ChatMessage, ExtractedRecord, OcrResult
    from app.models.risk import RiskEvent

    cutoff = datetime.now() - timedelta(days=days)
    msg_ids = [
        r[0] for r in db.execute(
            select(ChatMessage.id).where(ChatMessage.created_at < cutoff)
        ).all()
    ]
    if not msg_ids:
        return {"deleted_messages": 0, "deleted_attachments": 0, "deleted_records": 0, "deleted_events": 0}

    # 附件（及其 OCR 结果、抽取记录、风险事件）先删
    att_ids = [
        r[0] for r in db.execute(
            select(Attachment.id).where(Attachment.message_id.in_(msg_ids))
        ).all()
    ]
    n_events = db.execute(delete(RiskEvent).where(RiskEvent.message_id.in_(msg_ids))).rowcount or 0
    n_ocr = 0
    n_records = 0
    if att_ids:
        n_ocr = db.execute(delete(OcrResult).where(OcrResult.attachment_id.in_(att_ids))).rowcount or 0
        n_records = db.execute(delete(ExtractedRecord).where(ExtractedRecord.attachment_id.in_(att_ids))).rowcount or 0
        db.execute(delete(Attachment).where(Attachment.id.in_(att_ids)))
    n_msg = db.execute(delete(ChatMessage).where(ChatMessage.id.in_(msg_ids))).rowcount or 0
    db.commit()
    return {
        "deleted_messages": n_msg,
        "deleted_attachments": len(att_ids),
        "deleted_records": n_records,
        "deleted_events": n_events,
    }
