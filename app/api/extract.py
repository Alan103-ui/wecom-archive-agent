"""
app/api/extract.py — 抽取路线对比与实验开关

路由前缀 /extract（在 api/__init__.py 注册）。
  GET  /extract/modes           当前抽取模式 + 视觉模型绑定状态
  POST /extract/set-mode        切换抽取模式（ocr_llm / vision）
  POST /extract/compare         对同一批单据跑两条路线并对比
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.model_config import ROLE_EXTRACT_VISION, ROLE_LABELS
from app.services.extract import compare as compare_svc
from app.services.extract.compare import (
    EXTRACT_MODE_KEY,
    MODE_OCR_LLM,
    MODE_VISION,
)
from app.services.kv_store import get_setting, set_setting
from app.services.llm.client import get_model_for_role

router = APIRouter()


def _vision_configured() -> bool:
    return get_model_for_role(ROLE_EXTRACT_VISION, fallback=False) is not None


class SetModeIn(BaseModel):
    mode: str


class CompareIn(BaseModel):
    attachment_ids: list[str] | None = None
    sample_size: int = 5


@router.get("/modes", summary="当前抽取模式与视觉模型状态")
def get_modes():
    mode = get_setting(EXTRACT_MODE_KEY, MODE_OCR_LLM)
    vision_cfg = get_model_for_role(ROLE_EXTRACT_VISION, fallback=False)
    return {
        "current_mode": mode,
        "available_modes": [
            {"key": MODE_OCR_LLM, "label": "OCR + 文本模型（现状）"},
            {"key": MODE_VISION, "label": "视觉模型直接看图（实验）"},
        ],
        "vision": {
            "role": ROLE_EXTRACT_VISION,
            "role_label": ROLE_LABELS.get(ROLE_EXTRACT_VISION, ROLE_EXTRACT_VISION),
            "configured": vision_cfg is not None,
            "model": vision_cfg.model if vision_cfg else None,
            "name": vision_cfg.name if vision_cfg else None,
        },
    }


@router.post("/set-mode", summary="切换抽取模式（实验开关）")
def set_mode(body: SetModeIn):
    if body.mode not in (MODE_OCR_LLM, MODE_VISION):
        from fastapi import HTTPException

        raise HTTPException(400, f"mode 必须为 {MODE_OCR_LLM} 或 {MODE_VISION}")
    if body.mode == MODE_VISION and not _vision_configured():
        from fastapi import HTTPException

        raise HTTPException(400, "切换视觉模式前，请先到「模型配置」添加连接并勾选「视觉抽取(多模态)」")
    set_setting(EXTRACT_MODE_KEY, body.mode)
    return {"message": f"抽取模式已切换为 {body.mode}", "mode": body.mode}


@router.post("/compare", summary="两条抽取路线对比实验")
def run_compare(body: CompareIn, db: Session = Depends(get_db)):
    result = compare_svc.compare_routes(
        db,
        attachment_ids=body.attachment_ids,
        sample_size=body.sample_size,
    )
    return result
