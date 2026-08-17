"""
app/services/extract/extractor.py — 按模板把 OCR 文本抽成结构化字段

提示词工程要点（针对本地 14B 模型调优）：
  1. 明确给出字段清单与类型，不让模型自由发挥字段名
  2. 强调"原文没有就填 null"，遏制编造——这是结构化抽取最大的风险
  3. 要求同时返回自评置信度，便于前端标出需人工复核的记录
  4. OCR 文本过长时截断，避免超出上下文导致输出被截断
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings
from app.models.entities import ExtractTemplate
from app.services.extract.llm import LlmError, chat_json, chat_json_vision, get_model_for_role

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

# 送进模型的 OCR 文本上限（字符）。num_ctx=8192 token，中文约 1.5 字/token
MAX_TEXT_CHARS = 6000

SYSTEM_PROMPT = (
    "你是一个严谨的单据信息抽取引擎。你的唯一任务是从 OCR 文本中提取指定字段，"
    "并输出严格的 JSON。你绝不编造原文中不存在的信息。"
)


@dataclass
class ExtractOutcome:
    success: bool
    fields: dict[str, Any] = field(default_factory=dict)
    confidence: float | None = None
    model: str = ""
    duration_ms: int = 0
    error: str | None = None
    template_name: str | None = None


def _build_field_spec(fields_schema: list[dict]) -> str:
    lines = []
    for f in fields_schema:
        key = f.get("key")
        if not key:
            continue
        label = f.get("label", key)
        ftype = f.get("type", "string")
        desc = f.get("desc", "")
        line = f'  - "{key}"（{label}，类型 {ftype}）'
        if desc:
            line += f"：{desc}"
        # 数组类型：若定义了 items 子结构，给出每个元素应含的字段
        if ftype == "array" and f.get("items"):
            subs = []
            for it in f["items"]:
                ik = it.get("key")
                if not ik:
                    continue
                subs.append(f'{ik}({it.get("label", ik)}:{it.get("type", "string")})')
            if subs:
                line += "；每个元素为对象，含字段：" + " / ".join(subs)
        lines.append(line)
    return "\n".join(lines)


def _build_prompt(template: ExtractTemplate, ocr_text: str) -> str:
    text = (ocr_text or "").strip()
    truncated = False
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        truncated = True

    spec = _build_field_spec(template.fields_schema or [])
    keys = [f["key"] for f in (template.fields_schema or []) if f.get("key")]

    parts = [
        f"请从下面的 OCR 识别文本中，提取「{template.name}」的结构化信息。",
        "",
        "需要提取的字段：",
        spec,
        "",
        "输出要求：",
        f'1. 只输出一个 JSON 对象，顶层必须包含 "fields" 和 "confidence" 两个键。',
        f'2. "fields" 是对象，键必须严格是：{json.dumps(keys, ensure_ascii=False)}，不要增删键名。',
        '3. 原文中找不到的字段，值填 null，绝对不要猜测或编造。',
        '4. 金额、数量等数值类字段输出纯数字（不要带货币符号、单位、千分位逗号）。',
        '5. 日期统一格式化为 YYYY-MM-DD。',
        '6. "confidence" 是 0 到 1 之间的小数，表示你对本次抽取整体准确度的自评。',
        '7. 类型为 array 的字段【必须】输出为 JSON 数组（即使只有一行也要用 [] 包裹），'
        '数组元素是对象，对象字段严格按该字段说明中的"每个元素含字段"来组织；'
        '绝对不要把多行内容合并成一个字符串。',
    ]

    if template.prompt_extra:
        parts += ["", f"补充规则：{template.prompt_extra}"]

    if truncated:
        parts += ["", "（注意：以下 OCR 文本因过长已被截断）"]

    parts += [
        "",
        "OCR 文本如下：",
        "-----",
        text,
        "-----",
    ]
    return "\n".join(parts)


def _coerce(value: Any, ftype: str) -> Any:
    """把模型输出的值归一化到声明的类型，失败则保留原值（不丢数据）"""
    if value is None or value == "":
        return None

    if ftype == "number":
        if isinstance(value, (int, float)):
            return value
        s = str(value).strip()
        # 去掉货币符号、千分位、单位后缀
        for ch in ["￥", "¥", "$", ",", " ", "元", "吨", "个"]:
            s = s.replace(ch, "")
        try:
            f = float(s)
            return int(f) if f.is_integer() else f
        except ValueError:
            return value

    if ftype == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else [value]
            except json.JSONDecodeError:
                return [value]
        return [value]

    if ftype == "object":
        return value if isinstance(value, dict) else {"value": value}

    if ftype in ("string", "date"):
        return value if isinstance(value, str) else str(value)

    return value


def extract(template: ExtractTemplate, ocr_text: str) -> ExtractOutcome:
    """执行一次结构化抽取。不抛异常，错误封装在返回值里"""
    if not settings.EXTRACT_ENABLED:
        return ExtractOutcome(success=False, error="结构化抽取已在配置中关闭")

    if not (ocr_text or "").strip():
        return ExtractOutcome(
            success=False, error="OCR 文本为空，跳过抽取", template_name=template.name
        )

    t0 = time.time()
    prompt = _build_prompt(template, ocr_text)

    try:
        raw = chat_json(prompt, system=SYSTEM_PROMPT)
    except LlmError as e:
        return ExtractOutcome(
            success=False,
            error=str(e),
            duration_ms=int((time.time() - t0) * 1000),
            model=settings.OLLAMA_MODEL,
            template_name=template.name,
        )

    # 模型可能直接返回 fields 内容而没有外层包装，两种都兼容
    if isinstance(raw, dict) and "fields" in raw and isinstance(raw["fields"], dict):
        fields_raw = raw["fields"]
        confidence = raw.get("confidence")
    elif isinstance(raw, dict):
        fields_raw = raw
        confidence = raw.pop("confidence", None) if "confidence" in raw else None
    else:
        return ExtractOutcome(
            success=False,
            error=f"模型输出结构异常（期望对象，实际 {type(raw).__name__}）",
            duration_ms=int((time.time() - t0) * 1000),
            model=settings.OLLAMA_MODEL,
            template_name=template.name,
        )

    # 按 schema 归一化，并剔除模型自造的多余键
    schema = template.fields_schema or []
    normalized: dict[str, Any] = {}
    for f in schema:
        key = f.get("key")
        if not key:
            continue
        normalized[key] = _coerce(fields_raw.get(key), f.get("type", "string"))

    try:
        conf = float(confidence) if confidence is not None else None
        if conf is not None:
            conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = None

    filled = sum(1 for v in normalized.values() if v not in (None, "", [], {}))
    logger.info(
        "抽取完成 模板=%s 字段 %d/%d 置信度=%s 耗时=%dms",
        template.name, filled, len(normalized), conf, int((time.time() - t0) * 1000),
    )

    return ExtractOutcome(
        success=True,
        fields=normalized,
        confidence=conf,
        model=settings.OLLAMA_MODEL,
        duration_ms=int((time.time() - t0) * 1000),
        template_name=template.name,
    )


# ---------------------------------------------------------------- 视觉抽取路线
VISION_SYSTEM_PROMPT = (
    "你是一个严谨的单据信息抽取引擎，能够直接阅读图片。你的唯一任务是从图片中提取指定字段，"
    "并输出严格的 JSON。你绝不编造图片中不存在的信息。"
)


def _build_vision_prompt(template: ExtractTemplate) -> str:
    """与 _build_prompt 同构，但省略 OCR 文本块——信息来自图片本身。"""
    spec = _build_field_spec(template.fields_schema or [])
    keys = [f["key"] for f in (template.fields_schema or []) if f.get("key")]

    parts = [
        f"请从下面的图片中，提取「{template.name}」的结构化信息。",
        "",
        "需要提取的字段：",
        spec,
        "",
        "输出要求：",
        f'1. 只输出一个 JSON 对象，顶层必须包含 "fields" 和 "confidence" 两个键。',
        f'2. "fields" 是对象，键必须严格是：{json.dumps(keys, ensure_ascii=False)}，不要增删键名。',
        '3. 图片中找不到的字段，值填 null，绝对不要猜测或编造。',
        '4. 金额、数量等数值类字段输出纯数字（不要带货币符号、单位、千分位逗号）。',
        '5. 日期统一格式化为 YYYY-MM-DD。',
        '6. "confidence" 是 0 到 1 之间的小数，表示你对本次抽取整体准确度的自评。',
        '7. 类型为 array 的字段【必须】输出为 JSON 数组（即使只有一行也要用 [] 包裹），'
        '数组元素是对象，对象字段严格按该字段说明中的"每个元素含字段"来组织；'
        '绝对不要把多行内容合并成一个字符串。',
    ]

    if template.prompt_extra:
        parts += ["", f"补充规则：{template.prompt_extra}"]

    return "\n".join(parts)


def _encode_image_for_vision(path: Path) -> tuple[str, str]:
    """读图并转 base64；PDF 渲染首页为 PNG。返回 (base64, media_type)。"""
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise LlmError(f"未安装 pymupdf，无法把 PDF 转图做视觉抽取：{e}")
        doc = fitz.open(str(path))
        if doc.page_count == 0:
            raise LlmError("PDF 无页面")
        pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
        import io

        buf = io.BytesIO(pix.tobytes("png"))
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/png"
    # 图片：直接 base64（按扩展名推断 media type，缺省 png）
    media_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".bmp": "image/bmp", ".webp": "image/webp", ".gif": "image/gif",
        ".tiff": "image/tiff",
    }.get(ext, "image/png")
    return base64.b64encode(path.read_bytes()).decode("ascii"), media_type


def extract_vision(
    template: ExtractTemplate, image_path: str | Path, role: str = "extract_vision"
) -> ExtractOutcome:
    """视觉抽取路线：图片直接送多模态模型，跳过前置 OCR。

    不抛异常，错误封装在返回值里。
    """
    if not settings.EXTRACT_ENABLED:
        return ExtractOutcome(success=False, error="结构化抽取已在配置中关闭")

    cfg = get_model_for_role(role, fallback=False)
    if cfg is None:
        return ExtractOutcome(
            success=False,
            error="未配置视觉抽取模型（请到「模型配置」添加连接并勾选「视觉抽取(多模态)」）",
            template_name=template.name,
        )

    path = Path(image_path)
    if not path.exists():
        return ExtractOutcome(success=False, error=f"文件不存在：{path}", template_name=template.name)

    t0 = time.time()
    try:
        image_b64, media_type = _encode_image_for_vision(path)
    except LlmError as e:
        return ExtractOutcome(success=False, error=str(e), template_name=template.name)
    except Exception as e:  # noqa: BLE001
        return ExtractOutcome(success=False, error=f"图片编码失败：{e}", template_name=template.name)

    prompt = _build_vision_prompt(template)
    try:
        raw = chat_json_vision(cfg, image_b64, prompt, system=VISION_SYSTEM_PROMPT, image_media_type=media_type)
    except LlmError as e:
        return ExtractOutcome(
            success=False, error=str(e), duration_ms=int((time.time() - t0) * 1000),
            model=cfg.model, template_name=template.name,
        )

    # 与 extract 一致的字段归一化与包装兼容
    if isinstance(raw, dict) and "fields" in raw and isinstance(raw["fields"], dict):
        fields_raw = raw["fields"]
        confidence = raw.get("confidence")
    elif isinstance(raw, dict):
        fields_raw = raw
        confidence = raw.pop("confidence", None) if "confidence" in raw else None
    else:
        return ExtractOutcome(
            success=False,
            error=f"视觉模型输出结构异常（期望对象，实际 {type(raw).__name__}）",
            duration_ms=int((time.time() - t0) * 1000),
            model=cfg.model, template_name=template.name,
        )

    schema = template.fields_schema or []
    normalized: dict[str, Any] = {}
    for f in schema:
        key = f.get("key")
        if not key:
            continue
        normalized[key] = _coerce(fields_raw.get(key), f.get("type", "string"))

    try:
        conf = float(confidence) if confidence is not None else None
        if conf is not None:
            conf = max(0.0, min(1.0, conf))
    except (TypeError, ValueError):
        conf = None

    return ExtractOutcome(
        success=True,
        fields=normalized,
        confidence=conf,
        model=cfg.model,
        duration_ms=int((time.time() - t0) * 1000),
        template_name=template.name,
    )
