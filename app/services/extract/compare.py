"""
app/services/extract/compare.py — 两条抽取路线的对比实验

目标：用同一批单据，分别跑
  · 路线 A（现状）：OCR 文字 → 文本 LLM 结构化
  · 路线 B（视觉）：多模态 LLM 直接看图抽取（跳过 OCR）
对比两者的「字段覆盖率」「耗时」「成功率」，供决策者判断是否切换。

设计为纯诊断工具，不写库、不影响线上。若本地没有真实附件样本，
可临时生成合成样例图（PIL 渲染）用于跑通对比。
"""
from __future__ import annotations

import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.models.entities import Attachment, ExtractTemplate
from app.services.extract import extractor, templates
from app.services.extract.extractor import ExtractOutcome
from app.services.llm.client import get_model_for_role
from app.services.ocr import engine as ocr_engine

logger = logging.getLogger(__name__)

# 当前生产默认抽取模式；视觉模式走 extract_vision
MODE_OCR_LLM = "ocr_llm"
MODE_VISION = "vision"
EXTRACT_MODE_KEY = "extract_mode"


@dataclass
class DocCompare:
    doc_id: str | None
    name: str
    file_ext: str
    # 路线 A
    a_ok: bool = False
    a_fields: dict[str, Any] = field(default_factory=dict)
    a_coverage: float = 0.0
    a_latency_ms: int = 0
    a_error: str | None = None
    a_template: str | None = None
    # 路线 B
    b_ok: bool = False
    b_fields: dict[str, Any] = field(default_factory=dict)
    b_coverage: float = 0.0
    b_latency_ms: int = 0
    b_error: str | None = None
    b_template: str | None = None
    # 汇总
    note: str = ""


def _coverage(fields: dict[str, Any], schema: list[dict]) -> float:
    keys = [f.get("key") for f in schema if f.get("key")]
    if not keys:
        return 0.0
    filled = sum(1 for k in keys if fields.get(k) not in (None, "", [], {}))
    return round(filled / len(keys), 4)


def _pick_attachments(db, attachment_ids=None, sample_size=5) -> list[dict]:
    """挑出可用于对比的附件（已下载到本地、且是图片/PDF）。返回纯描述列表。"""
    conds = [Attachment.local_path.isnot(None), Attachment.local_path != ""]
    if attachment_ids:
        conds.append(Attachment.id.in_(attachment_ids))
    rows = (
        db.execute(
            select(Attachment)
            .where(*conds)
            .order_by(Attachment.created_at.desc())
            .limit(sample_size if not attachment_ids else len(attachment_ids))
        )
        .scalars()
        .all()
    )
    docs = []
    for a in rows:
        ext = (a.file_ext or "").lower()
        if ext not in settings.OCR_IMAGE_EXTS and ext not in settings.OCR_PDF_EXTS:
            continue
        docs.append({
            "doc_id": a.id, "name": a.file_name or a.id,
            "local_path": a.local_path, "file_ext": ext,
        })
    return docs


def _generate_samples(n: int, out_dir: Path) -> list[dict]:
    """临时生成合成样例图，让对比工具在没有真实附件时也能跑。

    尽力用系统中文字体渲染；若找不到则退化为 ASCII，此时两路线都会落到通用模板。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    font = None
    for cand in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        p = Path(cand)
        if p.exists():
            try:
                from PIL import ImageFont

                font = ImageFont.truetype(str(p), 28)
                break
            except Exception:  # noqa: BLE001
                continue

    docs: list[dict] = []
    # 三种单据，尽量命中真实模板关键词，让路线 A 也能匹配到专用模板
    samples_text = [
        ("送货单", "送货单\n供应商：测试物资公司\n收货单位：第一车间\n合计金额：12800\n物料：螺栓 规格M10 数量200"),
        ("增值税发票", "增值税发票\n销售方：示例商贸有限公司\n价税合计：5600.00\n开票日期：2026-08-01\n纳税人识别号：91310000XXXX"),
        ("生产日报", "生产日报\n车间：二车间\n计划产量：100\n实际产量：95\n完成率：95%\n记录人：张三"),
    ]
    from PIL import Image, ImageDraw

    for i in range(n):
        title, text = samples_text[i % len(samples_text)]
        img = Image.new("RGB", (900, 600), "white")
        draw = ImageDraw.Draw(img)
        if font:
            draw.text((40, 40), text, fill="black", font=font)
        else:
            draw.text((40, 40), text, fill="black")
        path = out_dir / f"sample_{uuid.uuid4().hex[:8]}.png"
        img.save(path)
        docs.append({
            "doc_id": None, "name": f"合成样例-{title}",
            "local_path": str(path), "file_ext": ".png",
        })
    return docs


def compare_routes(
    db,
    attachment_ids=None,
    sample_size: int = 5,
    vision_role: str = "extract_vision",
    generate_if_empty: bool = True,
) -> dict:
    """对同一批单据跑两条抽取路线并对比。返回结构化结果。"""
    vision_cfg = get_model_for_role(vision_role, fallback=False)
    vision_available = vision_cfg is not None

    docs = _pick_attachments(db, attachment_ids=attachment_ids, sample_size=sample_size)
    generated = False
    if not docs and generate_if_empty:
        tmp = Path(tempfile.gettempdir()) / "wecom_extract_samples"
        docs = _generate_samples(max(sample_size, 3), tmp)
        generated = True

    results: list[DocCompare] = []
    for d in docs:
        path = Path(d["local_path"])
        ext = d["file_ext"]
        cmp = DocCompare(doc_id=d["doc_id"], name=d["name"], file_ext=ext)

        # ---- 路线 A：OCR → 文本 LLM ----
        ocr_text = ""
        template: ExtractTemplate | None = None
        try:
            ocr_out = ocr_engine.recognize(path)
            if ocr_out.success:
                ocr_text = ocr_out.text or ""
            else:
                cmp.a_error = ocr_out.error
        except Exception as e:  # noqa: BLE001
            cmp.a_error = f"OCR 异常：{e}"

        if ocr_text.strip():
            template = templates.match_template(db, ocr_text, ext)
        if template is None:
            # 仍给个兜底模板，保证两路线公平可比
            template = templates.match_template(db, "", ext)
        cmp.a_template = template.name if template else None

        if template is not None and not cmp.a_error:
            t0 = time.time()
            out: ExtractOutcome = extractor.extract(template, ocr_text)
            cmp.a_latency_ms = out.duration_ms or int((time.time() - t0) * 1000)
            if out.success:
                cmp.a_ok = True
                cmp.a_fields = out.fields
                cmp.a_coverage = _coverage(out.fields, template.fields_schema or [])
            else:
                cmp.a_error = out.error
        elif template is None:
            cmp.a_error = cmp.a_error or "无可用抽取模板"

        # ---- 路线 B：视觉 LLM 直接看图 ----
        if not vision_available:
            cmp.b_error = "未配置视觉抽取模型（请到「模型配置」添加连接并勾选「视觉抽取(多模态)」）"
        elif template is None:
            cmp.b_error = cmp.b_error or "无可用抽取模板"
        else:
            cmp.b_template = template.name
            t0 = time.time()
            out = extractor.extract_vision(template, path, role=vision_role)
            cmp.b_latency_ms = out.duration_ms or int((time.time() - t0) * 1000)
            if out.success:
                cmp.b_ok = True
                cmp.b_fields = out.fields
                cmp.b_coverage = _coverage(out.fields, template.fields_schema or [])
            else:
                cmp.b_error = out.error

        # 简注：哪条路线覆盖率更高
        if cmp.a_ok and cmp.b_ok:
            if cmp.b_coverage > cmp.a_coverage:
                cmp.note = "视觉路线覆盖更全"
            elif cmp.a_coverage > cmp.b_coverage:
                cmp.note = "OCR 路线覆盖更全"
            else:
                cmp.note = "两条路线覆盖率持平"
        elif cmp.b_ok and not cmp.a_ok:
            cmp.note = "仅视觉路线成功"
        elif cmp.a_ok and not cmp.b_ok:
            cmp.note = "仅 OCR 路线成功"

        results.append(cmp)

    # ---- 聚合 ----
    a_cov = [r.a_coverage for r in results if r.a_ok]
    b_cov = [r.b_coverage for r in results if r.b_ok]
    a_lat = [r.a_latency_ms for r in results if r.a_ok]
    b_lat = [r.b_latency_ms for r in results if r.b_ok]

    avg = lambda xs: round(sum(xs) / len(xs), 4) if xs else None

    summary = {
        "doc_count": len(results),
        "generated_samples": generated,
        "vision_available": vision_available,
        "vision_model": vision_cfg.model if vision_cfg else None,
        "route_a": {
            "success": sum(1 for r in results if r.a_ok),
            "avg_coverage": avg(a_cov),
            "avg_latency_ms": avg(a_lat),
        },
        "route_b": {
            "success": sum(1 for r in results if r.b_ok),
            "avg_coverage": avg(b_cov),
            "avg_latency_ms": avg(b_lat),
        },
    }

    return {
        "summary": summary,
        "details": [
            {
                "doc_id": r.doc_id, "name": r.name, "file_ext": r.file_ext,
                "a": {"ok": r.a_ok, "coverage": r.a_coverage, "latency_ms": r.a_latency_ms,
                      "template": r.a_template, "error": r.a_error, "fields": r.a_fields},
                "b": {"ok": r.b_ok, "coverage": r.b_coverage, "latency_ms": r.b_latency_ms,
                      "template": r.b_template, "error": r.b_error, "fields": r.b_fields},
                "note": r.note,
            }
            for r in results
        ],
    }
