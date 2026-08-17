"""
app/api/records.py — 结构化业务数据接口（本项目的最终产物）

除常规增删改查外，提供两个「让数据真正可用」的能力：
  1. /records/flatten  把 fields_json 按模板展开成宽表（前端表格直接渲染）
  2. /records/export   导出 Excel，可直接给业务同事用
"""
from __future__ import annotations

import io
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Text, cast, func, select
from sqlalchemy.orm import Session

from app.api.schemas import ActionResult, Page, RecordOut, RecordUpdate
from app.db.database import get_db
from app.models.entities import ExtractedRecord, ExtractTemplate

router = APIRouter()


def _build_conds(
    room_id: str | None,
    template_name: str | None,
    status: str | None,
    reviewed: bool | None,
    start: datetime | None,
    end: datetime | None,
    keyword: str | None,
) -> list:
    conds = []
    if room_id:
        conds.append(ExtractedRecord.room_id == room_id)
    if template_name:
        conds.append(ExtractedRecord.template_name == template_name)
    if status:
        conds.append(ExtractedRecord.status == status)
    if reviewed is not None:
        conds.append(ExtractedRecord.reviewed.is_(reviewed))
    if start:
        conds.append(ExtractedRecord.biz_time >= start)
    if end:
        conds.append(ExtractedRecord.biz_time <= end)
    if keyword:
        # fields_json 在 SQLite 里是 TEXT，在 PG 里是 JSON。
        # 统一转成文本再模糊匹配，牺牲一点性能换两库通用。
        conds.append(cast(ExtractedRecord.fields_json, Text).ilike(f"%{keyword}%"))
    return conds


@router.get("/records", response_model=Page[RecordOut], summary="结构化数据列表")
def list_records(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    room_id: str | None = None,
    template_name: str | None = None,
    status: str | None = None,
    reviewed: bool | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    keyword: str | None = Query(None, description="在抽取字段里模糊搜索"),
):
    conds = _build_conds(room_id, template_name, status, reviewed, start, end, keyword)
    total = db.execute(select(func.count(ExtractedRecord.id)).where(*conds)).scalar_one()
    rows = (
        db.execute(
            select(ExtractedRecord)
            .where(*conds)
            .order_by(ExtractedRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return Page[RecordOut](
        total=total, page=page, page_size=page_size,
        items=[RecordOut.model_validate(r) for r in rows],
    )


@router.get("/records/flatten", summary="按模板展开成宽表")
def flatten_records(
    db: Session = Depends(get_db),
    template_name: str = Query(..., description="必须指定模板，不同模板字段不同"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    room_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
):
    """
    返回 {columns, rows}：
      columns = [{key,label,type}]  取自模板定义，保证列顺序稳定
      rows    = [{__id, __biz_time, 字段key: 值, ...}]
    数组/对象类型的值序列化成紧凑字符串，避免前端表格塌陷。
    """
    tpl = db.execute(
        select(ExtractTemplate).where(ExtractTemplate.name == template_name)
    ).scalar_one_or_none()
    if tpl is None:
        raise HTTPException(404, f"模板不存在：{template_name}")

    conds = [ExtractedRecord.template_name == template_name, ExtractedRecord.status == "done"]
    if room_id:
        conds.append(ExtractedRecord.room_id == room_id)
    if start:
        conds.append(ExtractedRecord.biz_time >= start)
    if end:
        conds.append(ExtractedRecord.biz_time <= end)

    total = db.execute(select(func.count(ExtractedRecord.id)).where(*conds)).scalar_one()
    records = (
        db.execute(
            select(ExtractedRecord)
            .where(*conds)
            .order_by(ExtractedRecord.biz_time.desc().nullslast())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )

    columns = [
        {"key": f.get("key"), "label": f.get("label") or f.get("key"), "type": f.get("type", "string")}
        for f in (tpl.fields_schema or [])
        if f.get("key")
    ]

    rows = []
    for rec in records:
        fields = rec.fields_json or {}
        row: dict = {
            "__id": rec.id,
            "__attachment_id": rec.attachment_id,
            "__room_id": rec.room_id,
            "__biz_time": rec.biz_time.isoformat() if rec.biz_time else None,
            "__confidence": rec.confidence,
            "__reviewed": rec.reviewed,
        }
        for col in columns:
            row[col["key"]] = _stringify(fields.get(col["key"]))
        rows.append(row)

    return {
        "template": template_name,
        "total": total,
        "page": page,
        "page_size": page_size,
        "columns": columns,
        "rows": rows,
    }


def _stringify(value):
    """数组/对象压成一行字符串，标量原样返回"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(" ".join(f"{k}:{v}" for k, v in item.items() if v is not None))
            else:
                parts.append(str(item))
        return " | ".join(parts)
    if isinstance(value, dict):
        return "; ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


@router.get("/records/export", summary="导出 Excel")
def export_records(
    db: Session = Depends(get_db),
    template_name: str = Query(..., description="按模板导出，列即模板字段"),
    room_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(5000, ge=1, le=50000),
):
    try:
        from openpyxl import Workbook
    except ImportError:  # pragma: no cover
        raise HTTPException(500, "缺少 openpyxl，请先 pip install openpyxl")

    tpl = db.execute(
        select(ExtractTemplate).where(ExtractTemplate.name == template_name)
    ).scalar_one_or_none()
    if tpl is None:
        raise HTTPException(404, f"模板不存在：{template_name}")

    conds = [ExtractedRecord.template_name == template_name, ExtractedRecord.status == "done"]
    if room_id:
        conds.append(ExtractedRecord.room_id == room_id)
    if start:
        conds.append(ExtractedRecord.biz_time >= start)
    if end:
        conds.append(ExtractedRecord.biz_time <= end)

    records = (
        db.execute(
            select(ExtractedRecord)
            .where(*conds)
            .order_by(ExtractedRecord.biz_time.desc().nullslast())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    fields = [f for f in (tpl.fields_schema or []) if f.get("key")]
    wb = Workbook()
    ws = wb.active
    ws.title = template_name[:30] or "data"
    ws.append(["业务时间", "群ID", "置信度", "已复核"] + [f.get("label") or f["key"] for f in fields])

    for rec in records:
        data = rec.fields_json or {}
        ws.append(
            [
                rec.biz_time.strftime("%Y-%m-%d %H:%M:%S") if rec.biz_time else "",
                rec.room_id,
                rec.confidence,
                "是" if rec.reviewed else "否",
            ]
            + [_stringify(data.get(f["key"])) for f in fields]
        )

    for idx, width in enumerate([20, 24, 10, 8] + [18] * len(fields), start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    # HTTP 头只允许 latin-1，模板名是中文，必须按 RFC 5987 百分号编码；
    # 同时给一个 ASCII 兜底名，照顾不支持 filename* 的老客户端。
    fname = f"{template_name}_{datetime.now():%Y%m%d%H%M%S}.xlsx"
    quoted = quote(fname)
    ascii_fallback = f"export_{datetime.now():%Y%m%d%H%M%S}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quoted}'
            )
        },
    )


@router.get("/records/{record_id}", response_model=RecordOut, summary="结构化数据详情")
def get_record(record_id: str, db: Session = Depends(get_db)):
    rec = db.get(ExtractedRecord, record_id)
    if rec is None:
        raise HTTPException(404, "记录不存在")
    out = RecordOut.model_validate(rec)
    # 附带模板字段结构：详情页对「未抽取到的字段」也展示为空白行，而非整行缺失
    schema = None
    if rec.template_id:
        tpl = db.get(ExtractTemplate, rec.template_id)
        if tpl is not None:
            schema = tpl.fields_schema
            out.display_style = tpl.display_style
            out.scenario = tpl.scenario
    out.fields_schema = schema
    return out


@router.patch("/records/{record_id}", response_model=RecordOut, summary="人工复核/修正字段")
def update_record(record_id: str, payload: RecordUpdate, db: Session = Depends(get_db)):
    rec = db.get(ExtractedRecord, record_id)
    if rec is None:
        raise HTTPException(404, "记录不存在")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(rec, k, v)
    if "fields_json" in data and "reviewed" not in data:
        rec.reviewed = True  # 改过字段视同已复核
    db.commit()
    db.refresh(rec)
    return RecordOut.model_validate(rec)


@router.delete("/records/{record_id}", response_model=ActionResult, summary="删除记录")
def delete_record(record_id: str, db: Session = Depends(get_db)):
    rec = db.get(ExtractedRecord, record_id)
    if rec is None:
        raise HTTPException(404, "记录不存在")
    db.delete(rec)
    db.commit()
    return ActionResult(message="已删除")
