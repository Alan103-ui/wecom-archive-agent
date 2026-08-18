"""
app/api/attachments.py — 附件、OCR 结果、原图预览接口
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import (
    ActionResult,
    AttachmentOut,
    OcrResultDetail,
    OcrResultOut,
    Page,
)
from app.config import settings
from app.db.database import get_db
from app.models.entities import Attachment, OcrResult
from app.services import pipeline
from app.services.auth.rbac import require_perm

router = APIRouter()


@router.get("/attachments", response_model=Page[AttachmentOut], summary="附件列表", dependencies=[Depends(require_perm("attachments", "view"))])
def list_attachments(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    room_id: str | None = None,
    media_type: str | None = None,
    file_ext: str | None = None,
    download_status: str | None = None,
    ocr_status: str | None = None,
    extract_status: str | None = None,
    keyword: str | None = Query(None, description="文件名模糊搜索"),
):
    conds = []
    if room_id:
        conds.append(Attachment.room_id == room_id)
    if media_type:
        conds.append(Attachment.media_type == media_type)
    if file_ext:
        conds.append(Attachment.file_ext == file_ext)
    if download_status:
        conds.append(Attachment.download_status == download_status)
    if ocr_status:
        conds.append(Attachment.ocr_status == ocr_status)
    if extract_status:
        conds.append(Attachment.extract_status == extract_status)
    if keyword:
        conds.append(Attachment.file_name.ilike(f"%{keyword}%"))

    total = db.execute(select(func.count(Attachment.id)).where(*conds)).scalar_one()
    rows = (
        db.execute(
            select(Attachment)
            .where(*conds)
            .order_by(Attachment.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return Page[AttachmentOut](
        total=total, page=page, page_size=page_size,
        items=[AttachmentOut.model_validate(r) for r in rows],
    )


@router.get("/attachments/{attachment_id}", response_model=AttachmentOut, summary="附件详情", dependencies=[Depends(require_perm("attachments", "view"))])
def get_attachment(attachment_id: str, db: Session = Depends(get_db)):
    att = db.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(404, "附件不存在")
    return AttachmentOut.model_validate(att)


@router.get(
    "/attachments/{attachment_id}/ocr",
    response_model=OcrResultDetail,
    summary="附件最新 OCR 结果（含坐标块）",
    dependencies=[Depends(require_perm("attachments", "view"))],
)
def get_attachment_ocr(attachment_id: str, db: Session = Depends(get_db)):
    row = db.execute(
        select(OcrResult)
        .where(OcrResult.attachment_id == attachment_id)
        .order_by(OcrResult.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "该附件尚无 OCR 结果")
    return OcrResultDetail.model_validate(row)


@router.get("/attachments/{attachment_id}/file", summary="下载/预览原文件", dependencies=[Depends(require_perm("attachments", "view"))])
def download_attachment(attachment_id: str, db: Session = Depends(get_db)):
    att = db.get(Attachment, attachment_id)
    if att is None:
        raise HTTPException(404, "附件不存在")
    if not att.local_path:
        raise HTTPException(404, "文件尚未下载到本地")

    path = Path(att.local_path).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    # 防目录穿越：只允许读 MEDIA_ROOT 下的文件
    if media_root not in path.parents and path != media_root:
        raise HTTPException(403, "非法的文件路径")
    if not path.is_file():
        raise HTTPException(404, "文件已丢失")

    return FileResponse(path, filename=att.file_name or path.name)


@router.post(
    "/attachments/{attachment_id}/retry", response_model=ActionResult, summary="重跑附件",
    dependencies=[Depends(require_perm("attachments", "operate"))],
)
def retry_attachment(
    attachment_id: str,
    stage: str = Query("all", pattern="^(all|download|ocr|extract)$"),
    db: Session = Depends(get_db),
):
    ok = pipeline.retry_attachment(db, attachment_id, stage)
    if not ok:
        raise HTTPException(404, "附件不存在或阶段名不合法")
    return ActionResult(message=f"已重置 {stage} 阶段，等待下一轮流水线处理")


@router.get("/ocr-results", response_model=Page[OcrResultOut], summary="OCR 结果列表", dependencies=[Depends(require_perm("attachments", "view"))])
def list_ocr_results(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status: str | None = None,
    keyword: str | None = Query(None, description="OCR 全文模糊搜索"),
):
    conds = []
    if status:
        conds.append(OcrResult.status == status)
    if keyword:
        conds.append(OcrResult.text_content.ilike(f"%{keyword}%"))

    total = db.execute(select(func.count(OcrResult.id)).where(*conds)).scalar_one()
    rows = (
        db.execute(
            select(OcrResult)
            .where(*conds)
            .order_by(OcrResult.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return Page[OcrResultOut](
        total=total, page=page, page_size=page_size,
        items=[OcrResultOut.model_validate(r) for r in rows],
    )
