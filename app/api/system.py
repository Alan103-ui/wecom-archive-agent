"""
app/api/system.py — 健康检查、统计看板、手动触发、游标与调度管理
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.schemas import ActionResult, CursorOut, HealthOut
from app.collectors import get_collector, reset_collector
from app.config import settings
from app.db.database import get_db
from app.models.entities import (
    Attachment,
    ChatMessage,
    ChatRoom,
    ExtractedRecord,
    OcrResult,
    SyncCursor,
)
from app.models.risk import AlertLayer, RiskEvent, RiskRule
from app.services import pipeline
from app.services.extract import llm
from app.services.ocr import engine as ocr_engine
from app import scheduler as scheduler_mod

router = APIRouter()


@router.get("/health", response_model=HealthOut, summary="健康检查")
def health(db: Session = Depends(get_db)):
    # 数据库
    try:
        db.execute(text("SELECT 1"))
        db_info = {"ok": True, "dialect": "sqlite" if settings.is_sqlite else "postgresql"}
    except Exception as e:  # noqa: BLE001
        db_info = {"ok": False, "error": str(e)[:300]}

    # 采集器
    try:
        ok, detail = get_collector().health_check()
        collector_info = {"ok": ok, "mode": settings.COLLECTOR_MODE, "detail": detail}
    except Exception as e:  # noqa: BLE001
        collector_info = {"ok": False, "mode": settings.COLLECTOR_MODE, "detail": str(e)[:300]}

    # OCR / LLM 的原始状态字段各不相同，这里统一补出 ok + detail，
    # 让前端与监控只认这两个键，原始细节仍原样透出。
    ocr_raw = ocr_engine.engine_status()
    ocr_info = {
        **ocr_raw,
        "ok": bool(ocr_raw.get("available")),
        "detail": ocr_raw.get("error") or ocr_raw.get("engine") or "",
    }

    if settings.EXTRACT_ENABLED:
        llm_raw = llm.health_check()
        ready = bool(llm_raw.get("available") and llm_raw.get("model_ready"))
        llm_info = {
            **llm_raw,
            "ok": ready,
            "detail": llm_raw.get("error")
            or (
                f"{llm_raw.get('model')} 就绪"
                if ready
                else f"模型 {llm_raw.get('model')} 未安装（已装：{', '.join(llm_raw.get('installed') or []) or '无'}）"
            ),
        }
    else:
        llm_info = {"ok": False, "available": False, "detail": "结构化抽取已关闭"}

    overall = "ok" if db_info.get("ok") else "degraded"
    return HealthOut(
        status=overall,
        app=settings.APP_NAME,
        collector_mode=settings.COLLECTOR_MODE,
        database=db_info,
        collector=collector_info,
        ocr=ocr_info,
        llm=llm_info,
        scheduler=scheduler_mod.scheduler_status(),
    )


@router.get("/stats", summary="统计看板")
def stats(db: Session = Depends(get_db)):
    def _count(model) -> int:
        return db.execute(select(func.count()).select_from(model)).scalar_one()

    def _group(col) -> dict:
        return {
            (k or "unknown"): v
            for k, v in db.execute(select(col, func.count()).group_by(col)).all()
        }

    cursor = db.execute(
        select(SyncCursor).where(SyncCursor.name == "default")
    ).scalar_one_or_none()

    return {
        "totals": {
            "messages": _count(ChatMessage),
            "attachments": _count(Attachment),
            "ocr_results": _count(OcrResult),
            "records": _count(ExtractedRecord),
            "rooms": _count(ChatRoom),
            "risk_rules": _count(RiskRule),
            "risk_events": _count(RiskEvent),
            "alert_layers": _count(AlertLayer),
        },
        "message_by_type": _group(ChatMessage.msg_type),
        "attachment_download": _group(Attachment.download_status),
        "attachment_ocr": _group(Attachment.ocr_status),
        "attachment_extract": _group(Attachment.extract_status),
        "record_by_template": _group(ExtractedRecord.template_name),
        "record_by_status": _group(ExtractedRecord.status),
        "cursor": {
            "seq": cursor.seq if cursor else 0,
            "total_fetched": cursor.total_fetched if cursor else 0,
            "last_sync_at": cursor.last_sync_at.isoformat() if cursor and cursor.last_sync_at else None,
            "last_error": cursor.last_error if cursor else None,
        },
        "scheduler": scheduler_mod.scheduler_status(),
    }


# ---------------------------------------------------------------- 手动触发
@router.post("/sync", response_model=ActionResult, summary="手动触发一次增量拉取")
def trigger_sync(
    background: BackgroundTasks,
    wait: bool = Query(True, description="true=同步等待结果；false=后台跑立即返回"),
    max_rounds: int = Query(20, ge=1, le=200),
):
    if wait:
        result = pipeline.sync_messages(max_rounds=max_rounds)
        return ActionResult(message="同步完成", data=result)
    background.add_task(pipeline.sync_messages, max_rounds)
    return ActionResult(message="已在后台开始同步")


@router.post("/pipeline/run", response_model=ActionResult, summary="手动触发一轮附件处理")
def trigger_pipeline(
    background: BackgroundTasks,
    wait: bool = Query(False, description="OCR+LLM 较慢，默认后台执行"),
    batch_size: int | None = Query(None, ge=1, le=200),
):
    if wait:
        result = pipeline.process_attachments(batch_size=batch_size)
        return ActionResult(message="处理完成", data=result)
    background.add_task(pipeline.process_attachments, batch_size)
    return ActionResult(message="已在后台开始处理")


@router.post("/pipeline/reset-failed", response_model=ActionResult, summary="重置所有失败项")
def reset_failed(db: Session = Depends(get_db)):
    n = pipeline.reset_failed(db)
    return ActionResult(message=f"已重置 {n} 个失败附件", data={"count": n})


# ---------------------------------------------------------------- 游标
@router.get("/cursor", response_model=CursorOut, summary="查看同步游标")
def get_cursor(db: Session = Depends(get_db)):
    return CursorOut.model_validate(pipeline.get_cursor(db))


@router.post("/cursor", response_model=CursorOut, summary="重设游标（危险）")
def set_cursor(
    db: Session = Depends(get_db),
    seq: int = Query(..., ge=0, description="设为 0 表示从最早可用记录重新拉"),
    confirm: bool = Query(False, description="必须显式传 true 才生效"),
):
    """
    调低游标会重复拉取（有 msgid 唯一约束兜底，不会产生重复数据）；
    调高会永久跳过中间的消息——存档只保留 5 天，跳过就找不回来了，所以强制二次确认。
    """
    if not confirm:
        raise HTTPException(400, "该操作会影响数据完整性，请附加 confirm=true")
    cur = pipeline.get_cursor(db)
    old = cur.seq
    cur.seq = seq
    cur.last_error = None
    db.commit()
    db.refresh(cur)
    return CursorOut.model_validate(cur)


# ---------------------------------------------------------------- 调度与采集器
@router.get("/scheduler", summary="调度器状态")
def scheduler_status():
    return scheduler_mod.scheduler_status()


@router.post("/scheduler/{action}", response_model=ActionResult, summary="暂停/恢复调度")
def scheduler_control(action: str):
    if action == "pause":
        ok = scheduler_mod.pause_all()
    elif action == "resume":
        ok = scheduler_mod.resume_all()
    else:
        raise HTTPException(400, "action 只能是 pause 或 resume")
    if not ok:
        raise HTTPException(409, "调度器未启动")
    return ActionResult(message=f"调度器已{'暂停' if action == 'pause' else '恢复'}")


@router.post("/collector/reload", response_model=ActionResult, summary="重载采集器")
def reload_collector():
    """改了 .env 里的 SDK 路径/密钥后，不重启进程即可生效"""
    reset_collector()
    try:
        ok, detail = get_collector().health_check()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"采集器初始化失败：{e}")
    return ActionResult(ok=ok, message=detail, data={"mode": settings.COLLECTOR_MODE})


@router.get("/config", summary="查看当前生效配置（脱敏）")
def view_config():
    def _mask(v: str) -> str:
        if not v:
            return ""
        return v[:4] + "****" + v[-2:] if len(v) > 8 else "****"

    return {
        "app_name": settings.APP_NAME,
        "port": settings.PORT,
        "collector_mode": settings.COLLECTOR_MODE,
        "database_url": settings.DATABASE_URL.split("://")[0] + "://***",
        "corp_id": _mask(settings.WECOM_CORP_ID),
        "archive_secret": _mask(settings.WECOM_ARCHIVE_SECRET),
        "sdk_path": settings.WECOM_SDK_PATH,
        "private_key_path": settings.WECOM_PRIVATE_KEY_PATH,
        "only_group_chat": settings.ONLY_GROUP_CHAT,
        "filter_room_ids": sorted(settings.filter_room_id_set),
        "media_root": settings.MEDIA_ROOT,
        "ocr_enabled": settings.OCR_ENABLED,
        "extract_enabled": settings.EXTRACT_ENABLED,
        "models": [
            {
                "id": c.id,
                "name": c.name,
                "provider": c.provider,
                "base_url": c.base_url,
                "model": c.model,
                "enabled": c.enabled,
                "is_default": c.is_default,
                "roles": list(c.roles or []),
            }
            for c in _model_configs()
        ],
        "scheduler": {
            "enabled": settings.SCHEDULER_ENABLED,
            "sync_interval": settings.SYNC_INTERVAL_SECONDS,
            "pipeline_interval": settings.PIPELINE_INTERVAL_SECONDS,
            "batch_size": settings.PIPELINE_BATCH_SIZE,
        },
    }


def _model_configs():
    from app.db.database import SessionLocal
    from app.models.model_config import ModelConfig

    db = SessionLocal()
    try:
        return db.query(ModelConfig).order_by(ModelConfig.created_at).all()
    finally:
        db.close()
