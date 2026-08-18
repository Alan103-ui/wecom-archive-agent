"""
app/scheduler.py — 定时调度

两个独立作业，互不阻塞：
    sync_job     每 SYNC_INTERVAL_SECONDS 秒拉一次新消息（快）
    pipeline_job 每 PIPELINE_INTERVAL_SECONDS 秒处理一批附件（慢：OCR + LLM）

关键配置：
    max_instances=1  同一作业不并发，防止两轮同时推游标 / 同一附件被处理两次
    coalesce=True    积压时只补跑一次，避免重启后瞬间爆发几十轮
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.services import pipeline

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

# 最近一次运行的结果，供 /api/system/health 与前端展示
last_run: dict = {
    "sync": {"at": None, "ok": None, "stats": None, "error": None},
    "pipeline": {"at": None, "ok": None, "stats": None, "error": None},
    "risk": {"at": None, "ok": None, "stats": None, "error": None},
    "timeout": {"at": None, "ok": None, "stats": None, "error": None},
    "retention": {"at": None, "ok": None, "stats": None, "error": None},
}


def _record(job: str, ok: bool, stats: dict | None = None, error: str | None = None) -> None:
    last_run[job] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "ok": ok,
        "stats": stats,
        "error": error,
    }


def sync_job() -> None:
    try:
        stats = pipeline.sync_messages()
        _record("sync", True, stats)
    except Exception as e:  # noqa: BLE001
        logger.exception("定时同步失败：%s", e)
        _record("sync", False, error=str(e)[:500])


def pipeline_job() -> None:
    try:
        stats = pipeline.process_attachments()
        _record("pipeline", True, stats)
    except Exception as e:  # noqa: BLE001
        logger.exception("定时流水线失败：%s", e)
        _record("pipeline", False, error=str(e)[:500])


def risk_job() -> None:
    try:
        stats = pipeline.risk_scan()
        _record("risk", True, stats)
    except Exception as e:  # noqa: BLE001
        logger.exception("定时风险扫描失败：%s", e)
        _record("risk", False, error=str(e)[:500])


def timeout_job() -> None:
    try:
        stats = pipeline.reply_timeout_scan()
        _record("timeout", True, stats)
    except Exception as e:  # noqa: BLE001
        logger.exception("定时超时回复扫描失败：%s", e)
        _record("timeout", False, error=str(e)[:500])


def retention_job() -> None:
    """合规留存清理：超期消息/附件/记录/风险事件（DATA_RETENTION_DAYS>0 时生效）。"""
    from app.db.database import SessionLocal
    from app.services.compliance import purge_expired

    db = SessionLocal()
    try:
        stats = purge_expired(db)
        _record("retention", True, stats)
        if any(stats.values()):
            logger.info("留存清理完成：%s", stats)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.exception("留存清理失败：%s", e)
        _record("retention", False, error=str(e)[:500])
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.SCHEDULER_ENABLED:
        logger.info("调度器已通过配置关闭（SCHEDULER_ENABLED=false）")
        return None
    if _scheduler is not None:
        return _scheduler

    sch = BackgroundScheduler(
        timezone="Asia/Shanghai",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    sch.add_job(
        sync_job,
        "interval",
        seconds=settings.SYNC_INTERVAL_SECONDS,
        id="sync_messages",
        name="增量拉取会话消息",
        next_run_time=datetime.now(),  # 启动即跑一次，别干等一个周期
    )
    sch.add_job(
        pipeline_job,
        "interval",
        seconds=settings.PIPELINE_INTERVAL_SECONDS,
        id="process_attachments",
        name="附件下载/OCR/结构化抽取",
    )
    sch.add_job(
        risk_job,
        "interval",
        seconds=settings.RISK_SCAN_INTERVAL_SECONDS,
        id="risk_scan",
        name="风险检测与分级预警",
        next_run_time=datetime.now(),
    )
    sch.add_job(
        timeout_job,
        "interval",
        seconds=settings.RISK_TIMEOUT_INTERVAL_SECONDS,
        id="reply_timeout_scan",
        name="超时回复提醒扫描",
        next_run_time=datetime.now(),
    )
    if settings.DATA_RETENTION_DAYS > 0:
        sch.add_job(
            retention_job,
            "interval",
            hours=settings.RETENTION_INTERVAL_HOURS,
            id="data_retention_purge",
            name="数据留存期清理",
            next_run_time=datetime.now(),
        )
    sch.start()
    _scheduler = sch
    logger.info(
        "调度器已启动：同步 %ds / 流水线 %ds",
        settings.SYNC_INTERVAL_SECONDS, settings.PIPELINE_INTERVAL_SECONDS,
    )
    return sch


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("调度器已停止")


def scheduler_status() -> dict:
    if _scheduler is None:
        return {"enabled": settings.SCHEDULER_ENABLED, "running": False, "jobs": [], "last_run": last_run}
    return {
        "enabled": True,
        "running": _scheduler.running,
        "jobs": [
            {
                "id": j.id,
                "name": j.name,
                "next_run": j.next_run_time.isoformat(timespec="seconds")
                if j.next_run_time
                else None,
            }
            for j in _scheduler.get_jobs()
        ],
        "last_run": last_run,
    }


def pause_all() -> bool:
    if _scheduler is None:
        return False
    _scheduler.pause()
    return True


def resume_all() -> bool:
    if _scheduler is None:
        return False
    _scheduler.resume()
    return True
