"""
app/services/extract/extractor.py — 按模板把 OCR 文本抽成结构化字段

提示词工程要点（针对本地 14B 模型调优）：
  1. 明确给出字段清单与类型，不让模型自由发挥字段名
  2. 强调"原文没有就填 null"，遏制编造——这是结构化抽取最大的风险
  3. 要求同时返回自评置信度，便于前端标出需人工复核的记录
  4. OCR 文本过长时截断，避免超出上下文导致输出被截断
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.models.entities import ExtractTemplate
from app.services.extract.llm import LlmError, chat_json

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
