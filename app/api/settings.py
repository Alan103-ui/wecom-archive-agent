"""
app/api/settings.py — 通用键值设置接口

保存"运行期可改、无需重启"的配置，例如超时回复提醒的开关/阈值/严重度。
读：GET /api/settings  返回全部 KV 为字典
写：PUT /api/settings   接受 {key: value} 批量 upsert
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy import select

from app.api.schemas import ActionResult
from app.db.database import get_db
from app.models.kv import KVSetting
from app.services.auth.rbac import require_perm

router = APIRouter()


@router.get("", summary="读取全部键值设置", dependencies=[Depends(require_perm("settings", "view"))])
def get_settings(db=Depends(get_db)):  # noqa: B008
    rows = db.execute(select(KVSetting)).scalars().all()
    return {r.key: r.value_json for r in rows}


@router.put("", summary="批量写入键值设置", dependencies=[Depends(require_perm("settings", "edit"))])
def put_settings(
    body: dict[str, Any] = Body(...),
    db=Depends(get_db),  # noqa: B008
):
    for k, v in body.items():
        row = db.get(KVSetting, k)
        if row is None:
            row = KVSetting(key=k, value_json=v)
            db.add(row)
        else:
            row.value_json = v
    db.commit()
    return ActionResult(message=f"已保存 {len(body)} 项设置", data=body)
