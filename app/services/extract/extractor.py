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
    warnings: list = field(default_factory=list)


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


def _call_with_retry(func, *args, **kwargs):
    """调用模型，遇 429/限流 指数退避重试，其它错误立即抛出。"""
    delays = [3, 8, 18, 35, 60]
    last_err = None
    for i in range(len(delays) + 1):
        try:
            return func(*args, **kwargs)
        except LlmError as e:
            msg = str(e)
            if any(k in msg for k in ("429", "rate", "限流", "RateLimit", "Too Many", "exceed")) and i < len(delays):
                time.sleep(delays[i])
                last_err = e
                continue
            raise
    if last_err:
        raise last_err
    raise LlmError("模型调用重试耗尽")


def _parse_model_output(raw, schema, template_name):
    """把模型原始输出归一化为 schema 字段字典；结构异常返回 (None, error, None)。"""
    if isinstance(raw, dict) and "fields" in raw and isinstance(raw["fields"], dict):
        fields_raw = raw["fields"]
        confidence = raw.get("confidence")
    elif isinstance(raw, dict):
        fields_raw = raw
        confidence = raw.pop("confidence", None) if "confidence" in raw else None
    else:
        return None, f"模型输出结构异常（期望对象，实际 {type(raw).__name__}）", None

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
    return normalized, None, conf


def _extract_single_text(template, text):
    """对一段 OCR 文本做一次抽取，返回 (fields, error, confidence, model)。"""
    prompt = _build_prompt(template, text)
    cfg = get_model_for_role("extract")
    model_name = cfg.model if cfg else (settings.OLLAMA_MODEL or "unknown")
    try:
        raw = _call_with_retry(chat_json, prompt, system=SYSTEM_PROMPT)
    except LlmError as e:
        return None, str(e), None, model_name
    fields, err, conf = _parse_model_output(raw, template.fields_schema or [], template.name)
    if err:
        return None, err, None, model_name
    return fields, None, conf, model_name


def _split_lines(lines, cap):
    """按行贪心切块，保证不在行中断，单块不超过 cap 字符。"""
    chunks, cur, n = [], [], 0
    for ln in lines:
        if n + len(ln) + 1 > cap and cur:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        chunks.append(cur)
    return chunks


def _find_total_field(schema):
    for f in schema:
        if f.get("type") != "number":
            continue
        k = (f.get("key") or "").lower()
        lbl = f.get("label") or ""
        if "total" in k or "合计" in lbl or "价税合计" in lbl or k in ("total", "total_amount", "grand_total"):
            return f
    return None


def _find_line_amount_key(items):
    if not items:
        return None
    for pref in ("amount", "金额", "price", "单价", "sum", "小计"):
        for it in items:
            k = (it.get("key") or "").lower()
            if pref in k:
                return it.get("key")
    return None


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    for ch in ["￥", "¥", "$", ",", " ", "元", "吨", "个", "%"]:
        s = s.replace(ch, "")
    try:
        return float(s)
    except ValueError:
        return None


def _validate_fields(template, fields):
    """行数 + 金额一致性校验，返回告警字符串列表。"""
    if not fields:
        return []
    warnings = []
    schema = template.fields_schema or []
    array_fields = [f for f in schema if f.get("type") == "array"]

    # 1. 明细数组非空
    for f in array_fields:
        arr = fields.get(f.get("key"))
        if not isinstance(arr, list) or len(arr) == 0:
            warnings.append(f"未抽取到「{f.get('label', f.get('key'))}」明细行")

    # 2. 金额一致性：各行明细金额之和 vs 合计字段
    total_field = _find_total_field(schema)
    if total_field:
        total = _num(fields.get(total_field["key"]))
        if total is not None:
            for f in array_fields:
                arr = fields.get(f.get("key"))
                if not isinstance(arr, list) or not arr:
                    continue
                line_amt = _find_line_amount_key(f.get("items"))
                if not line_amt:
                    continue
                s, ok = 0.0, 0
                for row in arr:
                    if not isinstance(row, dict):
                        continue
                    v = _num(row.get(line_amt))
                    if v is None and "qty" in row and "price" in row:
                        q, p = _num(row.get("qty")), _num(row.get("price"))
                        if q is not None and p is not None:
                            v = q * p
                    if v is not None:
                        s += v
                        ok += 1
                if ok:
                    diff = (abs(s - total) / abs(total)) if total else 0
                    if diff > 0.05:
                        warnings.append(
                            f"「{f.get('label')}」各行金额之和 {round(s, 2)} 与合计 "
                            f"{round(total, 2)} 不一致（差异 {round(diff * 100, 1)}%）"
                        )
    return warnings


def _extract_chunked(template, text):
    """长文本分段抽取：每段抽全部字段，数组字段 append 合并、单值取首个非空。"""
    lines = text.split("\n")
    chunks = _split_lines(lines, MAX_TEXT_CHARS)
    schema = template.fields_schema or []
    array_keys = [f["key"] for f in schema if f.get("type") == "array"]

    merged: dict[str, Any] = {}
    warnings: list[str] = []
    confs: list[float] = []
    model = settings.OLLAMA_MODEL
    any_ok = False
    first_err = None

    for i, ch in enumerate(chunks):
        fields, err, conf, mdl = _extract_single_text(template, "\n".join(ch))
        if err:
            warnings.append(f"第{i + 1}段抽取失败：{err[:80]}")
            if first_err is None:
                first_err = err
            continue
        any_ok = True
        if conf is not None:
            confs.append(conf)
        model = mdl
        for k, v in fields.items():
            if k in array_keys and isinstance(v, list):
                merged.setdefault(k, []).extend(v)
            else:
                if k not in merged or merged.get(k) in (None, "", [], {}):
                    merged[k] = v

    if not any_ok:
        return ExtractOutcome(
            success=False, error=first_err or "分段抽取全部失败", template_name=template.name
        )

    conf = round(sum(confs) / len(confs), 3) if confs else None
    warnings += _validate_fields(template, merged)
    if conf is not None and conf < 0.6:
        warnings.append(f"抽取置信度偏低（{round(conf * 100)}%），建议人工复核")
    return ExtractOutcome(
        success=True,
        fields=merged,
        confidence=conf,
        model=model,
        warnings=warnings,
        template_name=template.name,
    )


def extract(template: ExtractTemplate, ocr_text: str) -> ExtractOutcome:
    """执行一次结构化抽取。长文本自动分段，不丢明细行。不抛异常。"""
    if not settings.EXTRACT_ENABLED:
        return ExtractOutcome(success=False, error="结构化抽取已在配置中关闭")

    if not (ocr_text or "").strip():
        return ExtractOutcome(
            success=False, error="OCR 文本为空，跳过抽取", template_name=template.name
        )

    text = (ocr_text or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        logger.info("OCR 文本超长(%d 字)，启用分段抽取", len(text))
        return _extract_chunked(template, text)

    t0 = time.time()
    fields, err, conf, model = _extract_single_text(template, text)
    if err:
        return ExtractOutcome(
            success=False, error=err, duration_ms=int((time.time() - t0) * 1000),
            model=model, template_name=template.name,
        )
    warnings = _validate_fields(template, fields)
    if conf is not None and conf < 0.6:
        warnings.append(f"抽取置信度偏低（{round(conf * 100)}%），建议人工复核")
    filled = sum(1 for v in fields.values() if v not in (None, "", [], {}))
    logger.info(
        "抽取完成 模板=%s 字段 %d/%d 置信度=%s 耗时=%dms",
        template.name, filled, len(fields), conf, int((time.time() - t0) * 1000),
    )
    return ExtractOutcome(
        success=True,
        fields=fields,
        confidence=conf,
        model=model,
        duration_ms=int((time.time() - t0) * 1000),
        warnings=warnings,
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
        raw = _call_with_retry(
            chat_json_vision, cfg, image_b64, prompt,
            system=VISION_SYSTEM_PROMPT, image_media_type=media_type,
        )
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

    warnings = _validate_fields(template, normalized)
    if conf is not None and conf < 0.6:
        warnings.append(f"抽取置信度偏低（{round(conf * 100)}%），建议人工复核")
    return ExtractOutcome(
        success=True,
        fields=normalized,
        confidence=conf,
        model=cfg.model,
        duration_ms=int((time.time() - t0) * 1000),
        warnings=warnings,
        template_name=template.name,
    )
