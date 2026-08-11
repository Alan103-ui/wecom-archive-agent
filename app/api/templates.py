"""
app/api/templates.py — 抽取模板 CRUD 与在线调试

「加业务只加模板，不改代码」的入口就在这里。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    ActionResult,
    TemplateCreate,
    TemplateOut,
    TemplateTryRun,
    TemplateUpdate,
)
from app.db.database import get_db
from app.models.entities import ExtractedRecord, ExtractTemplate
from app.services.extract import extractor, templates as tpl_service

router = APIRouter()


@router.get("/templates", response_model=list[TemplateOut], summary="模板列表")
def list_templates(db: Session = Depends(get_db), enabled_only: bool = False):
    stmt = select(ExtractTemplate)
    if enabled_only:
        stmt = stmt.where(ExtractTemplate.enabled.is_(True))
    rows = db.execute(stmt.order_by(ExtractTemplate.priority.desc(), ExtractTemplate.name)).scalars().all()
    return [TemplateOut.model_validate(r) for r in rows]


@router.post("/templates", response_model=TemplateOut, summary="新建模板")
def create_template(payload: TemplateCreate, db: Session = Depends(get_db)):
    data = payload.model_dump()
    data["fields_schema"] = [f for f in data.get("fields_schema") or []]
    if not data["fields_schema"]:
        raise HTTPException(400, "至少要定义一个抽取字段")

    tpl = ExtractTemplate(**data)
    db.add(tpl)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"模板名已存在：{payload.name}")
    db.refresh(tpl)
    return TemplateOut.model_validate(tpl)


@router.get("/templates/{template_id}", response_model=TemplateOut, summary="模板详情")
def get_template(template_id: str, db: Session = Depends(get_db)):
    tpl = db.get(ExtractTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "模板不存在")
    return TemplateOut.model_validate(tpl)


@router.patch("/templates/{template_id}", response_model=TemplateOut, summary="修改模板")
def update_template(template_id: str, payload: TemplateUpdate, db: Session = Depends(get_db)):
    tpl = db.get(ExtractTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "模板不存在")

    data = payload.model_dump(exclude_unset=True)
    if "fields_schema" in data and data["fields_schema"] is not None:
        data["fields_schema"] = [
            f if isinstance(f, dict) else f.model_dump() for f in data["fields_schema"]
        ]
        if not data["fields_schema"]:
            raise HTTPException(400, "至少要定义一个抽取字段")

    for k, v in data.items():
        setattr(tpl, k, v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "模板名冲突")
    db.refresh(tpl)
    return TemplateOut.model_validate(tpl)


@router.delete("/templates/{template_id}", response_model=ActionResult, summary="删除模板")
def delete_template(template_id: str, db: Session = Depends(get_db)):
    tpl = db.get(ExtractTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "模板不存在")

    used = db.execute(
        select(ExtractedRecord.id).where(ExtractedRecord.template_id == template_id).limit(1)
    ).scalar_one_or_none()
    if used:
        # 已产生业务数据的模板不物理删除，避免历史记录失去字段定义
        tpl.enabled = False
        db.commit()
        return ActionResult(message="该模板已产生业务数据，已改为停用而非删除")

    db.delete(tpl)
    db.commit()
    return ActionResult(message="已删除")


@router.post("/templates/seed", response_model=ActionResult, summary="重新播种默认模板")
def seed_default_templates(db: Session = Depends(get_db)):
    added = tpl_service.seed_templates(db)
    return ActionResult(message=f"新增 {added} 个默认模板", data={"added": added})


@router.post("/templates/try-run", summary="在线调试：贴一段文本试抽")
def try_run(payload: TemplateTryRun, db: Session = Depends(get_db)):
    """
    不传 template_id 时走自动匹配，可用来验证关键词配得对不对。
    """
    if payload.template_id:
        tpl = db.get(ExtractTemplate, payload.template_id)
        if tpl is None:
            raise HTTPException(404, "模板不存在")
        matched_by = "manual"
    else:
        tpl = tpl_service.match_template(db, payload.text, None)
        if tpl is None:
            raise HTTPException(400, "没有可用模板（请先播种或新建）")
        matched_by = "auto"

    outcome = extractor.extract(tpl, payload.text)
    return {
        "template_id": tpl.id,
        "template_name": tpl.name,
        "matched_by": matched_by,
        "success": outcome.success,
        "fields": outcome.fields,
        "confidence": outcome.confidence,
        "model": outcome.model,
        "duration_ms": outcome.duration_ms,
        "error": outcome.error,
    }
