"""
app/services/pipeline.py — 端到端流水线编排

    采集 → 落库(去重) → 媒体下载 → OCR → 模板匹配 → LLM 抽取 → 结构化表

拆成两个独立阶段，各自可单独触发、单独重试：

  阶段一 sync_messages()      : 快，只做网络拉取与入库，保证不丢消息（存档仅存 5 天）
  阶段二 process_attachments(): 慢，OCR 与大模型推理，可离线补跑

这样设计的原因：OCR + LLM 一个附件可能要十几秒，
如果和拉取耦合在一起，一旦处理慢就会拖累游标推进，超过 5 天的消息就永久丢了。
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collectors import get_collector
from app.collectors.base import MediaRef, NormalizedMessage
from app.config import settings
from app.db.database import SessionLocal
from app.models.entities import (
    Attachment,
    ChatMessage,
    ChatRoom,
    ExtractedRecord,
    OcrResult,
    SyncCursor,
)
from app.services.extract import extractor, templates
from app.services.ocr import engine as ocr_engine
from app.services.risk import categories as cat
from app.services.risk import detector
from app.services.alert import sender
from app.models.risk import RiskEvent, RiskRule

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _safe_filename(name: str, fallback_ext: str = ".bin") -> str:
    """清洗文件名，防目录穿越与非法字符"""
    name = _SAFE_NAME.sub("_", (name or "").strip()) or f"file{fallback_ext}"
    return name[:120]


def _media_dest(room_id: str, msgid: str, media: MediaRef) -> Path:
    """按 群/日期 分目录存放，避免单目录几十万文件"""
    day = datetime.now().strftime("%Y%m%d")
    room_dir = _safe_filename(room_id or "single") or "single"
    fname = _safe_filename(media.file_name or f"{uuid.uuid4().hex}{media.file_ext or '.bin'}")
    # 加 msgid 短哈希前缀，防同名覆盖
    prefix = re.sub(r"\W", "", msgid)[-10:] or uuid.uuid4().hex[:10]
    return Path(settings.MEDIA_ROOT) / room_dir / day / f"{prefix}_{fname}"


# ==========================================================================
# 游标
# ==========================================================================
def get_cursor(db: Session, name: str = "default") -> SyncCursor:
    cur = db.execute(select(SyncCursor).where(SyncCursor.name == name)).scalar_one_or_none()
    if cur is None:
        cur = SyncCursor(name=name, seq=0)
        db.add(cur)
        db.commit()
        db.refresh(cur)
    return cur


# ==========================================================================
# 阶段一：拉取消息并落库
# ==========================================================================
def sync_messages(max_rounds: int = 20) -> dict:
    """
    增量拉取。循环直到没有新消息或达到轮次上限。

    :param max_rounds: 单次调用最多拉几轮，防止首次全量时把一轮跑成几小时
    """
    collector = get_collector()
    stats = {
        "fetched": 0, "saved": 0, "duplicated": 0, "attachments": 0,
        "rounds": 0, "start_seq": 0, "end_seq": 0, "errors": [],
    }

    db = SessionLocal()
    try:
        cursor = get_cursor(db)
        stats["start_seq"] = cursor.seq
        seq = cursor.seq
        room_filter = settings.filter_room_id_set

        # 关闭采集的群：跳过其新消息（已落库的历史数据保留，不删除）
        disabled_rooms = set(
            r[0] for r in db.execute(
                select(ChatRoom.room_id).where(ChatRoom.enabled == False)  # noqa: E712
            ).all()
        )

        for _ in range(max_rounds):
            stats["rounds"] += 1
            try:
                batch = collector.fetch(seq, settings.WECOM_FETCH_LIMIT)
            except Exception as e:  # noqa: BLE001
                msg = f"拉取失败(seq={seq})：{e}"
                logger.error(msg)
                stats["errors"].append(msg)
                cursor.last_error = str(e)[:500]
                db.commit()
                break

            if not batch:
                break

            stats["fetched"] += len(batch)
            max_seq = seq

            for nm in batch:
                max_seq = max(max_seq, nm.seq)

                # 过滤：只要群聊 / 只要白名单群
                if settings.ONLY_GROUP_CHAT and not nm.is_group:
                    continue
                if room_filter and nm.room_id not in room_filter:
                    continue

                # 关闭采集的群：跳过其新消息（历史已采集数据保留）
                if disabled_rooms and nm.room_id in disabled_rooms:
                    continue

                try:
                    saved = _save_message(db, nm)
                    if saved is None:
                        stats["duplicated"] += 1
                    else:
                        stats["saved"] += 1
                        stats["attachments"] += saved
                except Exception as e:  # noqa: BLE001
                    db.rollback()
                    msg = f"消息入库失败 msgid={nm.msgid}：{e}"
                    logger.error(msg)
                    stats["errors"].append(msg)

            # 游标必须推进到本批最大 seq，否则死循环
            seq = max_seq
            cursor.seq = seq
            cursor.total_fetched = (cursor.total_fetched or 0) + len(batch)
            cursor.last_sync_at = datetime.now()
            cursor.last_error = None
            db.commit()

            # 不足一批说明追上了
            if len(batch) < settings.WECOM_FETCH_LIMIT:
                break

        stats["end_seq"] = seq
        _refresh_room_stats(db)
    finally:
        db.close()

    logger.info(
        "同步完成 seq %d→%d 新增 %d 条（附件 %d）重复 %d",
        stats["start_seq"], stats["end_seq"], stats["saved"],
        stats["attachments"], stats["duplicated"],
    )
    return stats


def _save_message(db: Session, nm: NormalizedMessage) -> int | None:
    """写入一条消息及其附件。已存在返回 None，否则返回附件数"""
    exists = db.execute(
        select(ChatMessage.id).where(ChatMessage.msgid == nm.msgid)
    ).scalar_one_or_none()
    if exists:
        return None

    msg = ChatMessage(
        seq=nm.seq,
        msgid=nm.msgid,
        action=nm.action,
        msg_type=nm.msg_type,
        from_id=nm.from_id,
        to_list=nm.to_list,
        room_id=nm.room_id,
        msg_time_ms=nm.msg_time_ms,
        msg_time=nm.msg_time,
        content_text=nm.content_text,
        raw_json=nm.raw,
        attachment_count=len(nm.medias),
    )
    db.add(msg)
    db.flush()  # 拿到 msg.id

    for media in nm.medias:
        if media.media_type not in settings.MEDIA_DOWNLOAD_TYPES:
            continue
        db.add(
            Attachment(
                message_id=msg.id,
                room_id=nm.room_id,
                msgid=nm.msgid,
                media_type=media.media_type,
                sdkfileid=media.sdkfileid,
                file_name=media.file_name,
                file_ext=media.file_ext,
                file_size=media.file_size,
                md5sum=media.md5sum,
                # mock 模式带本地路径，仍走下载流程（拷贝），保持链路一致
                local_path=None,
                download_status="pending",
                # 不支持 OCR 的类型直接标 skipped，别让它堵在队列里
                ocr_status="pending" if ocr_engine.is_ocr_supported(media.file_ext) else "skipped",
                extract_status="pending"
                if ocr_engine.is_ocr_supported(media.file_ext)
                else "skipped",
            )
        )

    try:
        db.commit()
    except IntegrityError:
        # 并发下另一个线程抢先插入了同 msgid
        db.rollback()
        return None

    return len(nm.medias)


def _refresh_room_stats(db: Session) -> None:
    """刷新群档案统计"""
    rows = db.execute(
        select(
            ChatMessage.room_id,
            func.count(ChatMessage.id),
            func.max(ChatMessage.msg_time),
            func.sum(ChatMessage.attachment_count),
        )
        .where(ChatMessage.room_id != "")
        .group_by(ChatMessage.room_id)
    ).all()

    for room_id, cnt, last_at, att in rows:
        room = db.get(ChatRoom, room_id)
        if room is None:
            room = ChatRoom(room_id=room_id, name=None)
            db.add(room)
        room.msg_count = cnt or 0
        room.attachment_count = int(att or 0)
        room.last_msg_at = last_at
    db.commit()


# ==========================================================================
# 阶段二：附件处理（下载 → OCR → 抽取）
# ==========================================================================
def process_attachments(batch_size: int | None = None) -> dict:
    """处理一批待办附件。每个附件独立事务，互不影响"""
    batch_size = batch_size or settings.PIPELINE_BATCH_SIZE
    stats = {"picked": 0, "downloaded": 0, "ocr_done": 0, "extracted": 0, "failed": 0}

    db = SessionLocal()
    try:
        pending = (
            db.execute(
                select(Attachment)
                .where(
                    (Attachment.download_status.in_(["pending", "failed"]))
                    | (
                        (Attachment.download_status == "done")
                        & (Attachment.ocr_status.in_(["pending", "failed"]))
                    )
                    | (
                        (Attachment.ocr_status == "done")
                        & (Attachment.extract_status.in_(["pending", "failed"]))
                    )
                )
                .where(Attachment.download_retry < 3)
                .order_by(Attachment.created_at)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )
        stats["picked"] = len(pending)

        for att in pending:
            try:
                _process_one(db, att, stats)
            except Exception as e:  # noqa: BLE001
                db.rollback()
                stats["failed"] += 1
                logger.exception("附件处理异常 id=%s：%s", att.id, e)
    finally:
        db.close()

    if stats["picked"]:
        logger.info(
            "流水线一轮：取 %d 下载 %d OCR %d 抽取 %d 失败 %d",
            stats["picked"], stats["downloaded"], stats["ocr_done"],
            stats["extracted"], stats["failed"],
        )
    return stats


def _process_one(db: Session, att: Attachment, stats: dict) -> None:
    # ---------- 1. 下载 ----------
    if att.download_status in ("pending", "failed"):
        collector = get_collector()
        msg = db.get(ChatMessage, att.message_id)

        media = MediaRef(
            media_type=att.media_type,
            sdkfileid=att.sdkfileid,
            file_name=att.file_name,
            file_ext=att.file_ext,
            file_size=att.file_size,
            md5sum=att.md5sum,
            # mock 采集器需要 fixtures 原始路径：从 sdkfileid 反查
            local_path=_resolve_mock_path(att),
        )
        dest = _media_dest(att.room_id, att.msgid or (msg.msgid if msg else ""), media)

        att.download_status = "processing"
        db.commit()

        try:
            size = collector.download_media(media, str(dest))
            att.local_path = str(dest)
            att.file_size = size or att.file_size
            att.download_status = "done"
            att.download_error = None
            att.downloaded_at = datetime.now()
            stats["downloaded"] += 1
        except Exception as e:  # noqa: BLE001
            att.download_status = "failed"
            att.download_error = str(e)[:1000]
            att.download_retry = (att.download_retry or 0) + 1
            stats["failed"] += 1
            db.commit()
            logger.warning("附件下载失败 %s：%s", att.file_name, e)
            return
        db.commit()

    # ---------- 2. OCR ----------
    if att.download_status == "done" and att.ocr_status in ("pending", "failed"):
        if not settings.OCR_ENABLED:
            att.ocr_status = "skipped"
            att.extract_status = "skipped"
            db.commit()
            return

        if not ocr_engine.is_ocr_supported(att.file_ext):
            att.ocr_status = "skipped"
            att.extract_status = "skipped"
            db.commit()
            return

        att.ocr_status = "processing"
        db.commit()

        outcome = ocr_engine.recognize(att.local_path)
        db.add(
            OcrResult(
                attachment_id=att.id,
                engine=outcome.engine,
                status="done" if outcome.success else "failed",
                text_content=outcome.text,
                blocks_json=outcome.blocks_as_dict(),
                page_count=outcome.page_count,
                text_length=len(outcome.text or ""),
                avg_confidence=outcome.avg_confidence,
                duration_ms=outcome.duration_ms,
                error=outcome.error,
            )
        )

        if outcome.success:
            att.ocr_status = "done"
            stats["ocr_done"] += 1
        else:
            att.ocr_status = "failed"
            att.extract_status = "skipped"
            stats["failed"] += 1
            logger.warning("OCR 失败 %s：%s", att.file_name, outcome.error)
        db.commit()

    # ---------- 3. 结构化抽取 ----------
    if att.ocr_status == "done" and att.extract_status in ("pending", "failed"):
        if not settings.EXTRACT_ENABLED:
            att.extract_status = "skipped"
            db.commit()
            return

        latest = db.execute(
            select(OcrResult)
            .where(OcrResult.attachment_id == att.id, OcrResult.status == "done")
            .order_by(OcrResult.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if latest is None or not (latest.text_content or "").strip():
            att.extract_status = "skipped"
            db.commit()
            return

        tpl = templates.match_template(db, latest.text_content, att.file_ext)
        if tpl is None:
            att.extract_status = "skipped"
            db.commit()
            logger.info("无可用抽取模板，跳过 %s", att.file_name)
            return

        att.extract_status = "processing"
        db.commit()

        result = extractor.extract(tpl, latest.text_content)
        msg = db.get(ChatMessage, att.message_id)

        db.add(
            ExtractedRecord(
                message_id=att.message_id,
                attachment_id=att.id,
                room_id=att.room_id,
                msgid=att.msgid,
                template_id=tpl.id,
                template_name=tpl.name,
                status="done" if result.success else "failed",
                fields_json=result.fields,
                confidence=result.confidence,
                model=result.model,
                duration_ms=result.duration_ms,
                error=result.error,
                biz_time=msg.msg_time if msg else None,
            )
        )

        if result.success:
            att.extract_status = "done"
            stats["extracted"] += 1
        else:
            att.extract_status = "failed"
            stats["failed"] += 1
            logger.warning("抽取失败 %s：%s", att.file_name, result.error)
        db.commit()


def _resolve_mock_path(att: Attachment) -> str | None:
    """mock 模式下由 sdkfileid 反查 fixtures 文件路径"""
    if settings.COLLECTOR_MODE != "mock":
        return None
    if not att.sdkfileid.startswith("mock_sdkfileid_"):
        return None

    md5 = att.sdkfileid.replace("mock_sdkfileid_", "")
    fixture_dir = Path(settings.MEDIA_ROOT).parent / "fixtures"
    if not fixture_dir.exists():
        return None

    import hashlib

    for p in fixture_dir.iterdir():
        if p.is_file() and hashlib.md5(p.read_bytes()).hexdigest() == md5:
            return str(p)
    return None


# ==========================================================================
# 风险扫描（采集 → OCR/抽取 之后的独立阶段）
# ==========================================================================
def _assemble_text(db: Session, msg: ChatMessage) -> str:
    """拼装一条消息用于风险检测的文本：正文 + 其附件的 OCR 文本"""
    parts: list[str] = []
    if msg.content_text:
        parts.append(msg.content_text)
    for att in msg.attachments:
        if att.ocr_status == "done":
            ocr = db.execute(
                select(OcrResult)
                .where(OcrResult.attachment_id == att.id, OcrResult.status == "done")
                .order_by(OcrResult.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if ocr and ocr.text_content:
                parts.append(ocr.text_content)
    return "\n".join(parts)


def _resolve_layers(db: Session, hit, rule_id: str | None, severity: str) -> list[str]:
    """按规则显式路由或 severity 兜底，决定通知哪些管理层（有序）"""
    if rule_id:
        rule = db.get(RiskRule, rule_id)
        if rule and rule.alert_layers:
            return list(rule.alert_layers)
    return list(cat.DEFAULT_SEVERITY_LAYERS.get(severity, ["L1"]))


def detect_and_store(db: Session, msg: ChatMessage, text: str, rules: list[RiskRule]) -> list[RiskEvent]:
    """对一条消息做双引擎检测，归并去重后落库 + 触发分级预警"""
    hits = detector.scan(text, rules, msg.room_id)
    if not hits:
        return []

    # 按分类归并：优先保留关键词命中、严重度更高者
    by_cat: dict[str, object] = {}
    for h in hits:
        cur = by_cat.get(h.category)
        if cur is None:
            by_cat[h.category] = h
        else:
            prefer = (
                (h.detection_method == "keyword" and cur.detection_method != "keyword")
                or cat.SEVERITY_ORDER.get(h.severity, 0) > cat.SEVERITY_ORDER.get(cur.severity, 0)
            )
            if prefer:
                by_cat[h.category] = h

    events: list[RiskEvent] = []
    for category, hit in by_cat.items():
        # 同消息同分类只留一条（兼容并发扫描）
        exists = db.execute(
            select(RiskEvent.id).where(RiskEvent.message_id == msg.id, RiskEvent.category == category)
        ).scalar_one_or_none()
        if exists:
            continue

        event = RiskEvent(
            message_id=msg.id,
            room_id=msg.room_id,
            from_id=msg.from_id,
            rule_id=hit.rule_id,
            category=category,
            severity=hit.severity,
            detection_method=hit.detection_method,
            matched_keyword=hit.matched_keyword,
            snippet=hit.snippet,
            detail=hit.detail,
            biz_time=msg.msg_time,
            alert_status="unsent",
        )
        db.add(event)
        db.flush()

        layers = _resolve_layers(db, hit, hit.rule_id, hit.severity)
        try:
            event.alert_status = sender.dispatch_alert(db, event, layers)
        except Exception as e:  # noqa: BLE001
            logger.warning("预警投递异常 event=%s：%s", event.id, e)
            event.alert_status = "failed"
        events.append(event)
    return events


def risk_scan(batch_size: int | None = None) -> dict:
    """
    扫描尚未风险检测的消息，跑双引擎 → 落 RiskEvent → 分级预警。

    与同步/流水线解耦为独立作业，保证：
      - 不拖慢 seq 游标推进
      - 旧的已落库消息也能回填扫描（rescan 场景）
    """
    if not settings.RISK_ENABLED:
        return {"skipped": True, "scanned": 0, "events": 0, "alerts": 0}

    batch_size = batch_size or settings.RISK_SCAN_BATCH
    stats = {"scanned": 0, "events": 0, "alerts": 0, "errors": []}

    db = SessionLocal()
    try:
        rules = detector.load_rules(db)
        msgs = (
            db.execute(
                select(ChatMessage)
                .where(ChatMessage.risk_scanned == False)  # noqa: E712
                .where(ChatMessage.msg_type != "revoke")
                .order_by(ChatMessage.msg_time)
                .limit(batch_size)
            )
            .scalars()
            .all()
        )

        for m in msgs:
            stats["scanned"] += 1
            try:
                text = _assemble_text(db, m)
                events = detect_and_store(db, m, text, rules)
                stats["events"] += len(events)
                stats["alerts"] += sum(1 for e in events if e.alert_status in ("sent", "partial"))
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(str(e)[:200])
                logger.exception("风险扫描消息异常 msgid=%s：%s", m.msgid, e)
            m.risk_scanned = True

        db.commit()
    finally:
        db.close()

    if stats["scanned"]:
        logger.info(
            "风险扫描：扫 %d 条，命中 %d 事件，送达预警 %d 条",
            stats["scanned"], stats["events"], stats["alerts"],
        )
    return stats


def reply_timeout_scan() -> dict:
    """
    超时回复提醒：跨群聚合会话时间线，识别"客户消息后超时无员工回复"并落库预警。

    与逐条风险扫描（risk_scan）解耦为独立作业；按 (首条客户消息, 分类) 去重，
    重复运行不会重复建事件。配置（开关/阈值/严重度）优先读 KV，回落 config 默认。
    """
    if not settings.RISK_ENABLED:
        return {"skipped": True, "checked_rooms": 0, "events": 0, "errors": []}

    db = SessionLocal()
    try:
        from app.services.risk import timeout

        stats = timeout.scan_reply_timeouts(db)
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("超时回复扫描异常：%s", e)
        stats = {"skipped": False, "checked_rooms": 0, "events": 0,
                 "errors": [str(e)[:200]]}
    finally:
        db.close()

    if stats.get("events"):
        logger.info(
            "超时回复扫描：查 %d 群，命中 %d 条超时事件",
            stats.get("checked_rooms", 0), stats.get("events", 0),
        )
    return stats


def risk_rescan(db: Session, room_id: str | None = None, limit: int | None = None) -> int:
    """把已扫消息标记为待扫（回填/重扫）。返回重置条数"""
    stmt = update(ChatMessage).where(ChatMessage.risk_scanned == True)  # noqa: E712
    if room_id:
        stmt = stmt.where(ChatMessage.room_id == room_id)
    if limit:
        # SQLite 的 UPDATE..LIMIT 不支持，这里简单全量重置（群数量不会太大）
        pass
    result = db.execute(stmt.values(risk_scanned=False))
    db.commit()
    return result.rowcount or 0


# ==========================================================================
# 重跑
# ==========================================================================
def retry_attachment(db: Session, attachment_id: str, stage: str = "all") -> bool:
    """把附件的指定阶段状态重置为 pending，等待下一轮流水线处理"""
    att = db.get(Attachment, attachment_id)
    if att is None:
        return False

    if stage in ("all", "download"):
        att.download_status = "pending"
        att.download_retry = 0
        att.ocr_status = "pending"
        att.extract_status = "pending"
    elif stage == "ocr":
        att.ocr_status = "pending"
        att.extract_status = "pending"
    elif stage == "extract":
        att.extract_status = "pending"
    else:
        return False

    db.commit()
    return True


def reset_failed(db: Session) -> int:
    """把所有失败项重置为待处理（清空重试计数）"""
    result = db.execute(
        update(Attachment)
        .where(
            (Attachment.download_status == "failed")
            | (Attachment.ocr_status == "failed")
            | (Attachment.extract_status == "failed")
        )
        .values(download_retry=0)
    )
    db.execute(
        update(Attachment)
        .where(Attachment.download_status == "failed")
        .values(download_status="pending")
    )
    db.execute(
        update(Attachment).where(Attachment.ocr_status == "failed").values(ocr_status="pending")
    )
    db.execute(
        update(Attachment)
        .where(Attachment.extract_status == "failed")
        .values(extract_status="pending")
    )
    db.commit()
    return result.rowcount or 0
