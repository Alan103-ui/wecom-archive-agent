"""
app/api/schemas.py — 接口出入参模型

约定：
1. 列表接口统一返回 {total, page, page_size, items}，前端分页组件可直接复用。
2. ORM 对象用 model_config = ConfigDict(from_attributes=True) 直接转换，不手工搬字段。
3. 时间统一由 FastAPI 序列化成 ISO 字符串。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.compliance import mask_if_enabled

T = TypeVar("T")


def _mask_value(v: Any) -> Any:
    """递归脱敏：仅处理字符串，保留结构。"""
    if isinstance(v, str):
        return mask_if_enabled(v)
    if isinstance(v, list):
        return [_mask_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _mask_value(x) for k, x in v.items()}
    return v


class Page(BaseModel, Generic[T]):
    total: int = 0
    page: int = 1
    page_size: int = 20
    items: list[T] = Field(default_factory=list)


# ---------------------------------------------------------------- 消息
class AttachmentBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    media_type: str
    file_name: str | None = None
    file_ext: str | None = None
    file_size: int = 0
    download_status: str
    ocr_status: str
    extract_status: str


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    seq: int
    msgid: str
    action: str
    msg_type: str
    from_id: str
    from_name: str | None = None
    room_id: str
    msg_time: datetime | None = None
    content_text: str | None = None
    attachment_count: int = 0
    created_at: datetime

    @field_validator("content_text")
    @classmethod
    def _mask_content(cls, v: str | None) -> str | None:
        return mask_if_enabled(v)


class MessageDetail(MessageOut):
    raw_json: dict | None = None
    attachments: list[AttachmentBrief] = Field(default_factory=list)


# ---------------------------------------------------------------- 附件
class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    message_id: str
    room_id: str
    msgid: str
    media_type: str
    file_name: str | None = None
    file_ext: str | None = None
    file_size: int = 0
    md5sum: str | None = None
    local_path: str | None = None
    download_status: str
    download_error: str | None = None
    download_retry: int = 0
    downloaded_at: datetime | None = None
    ocr_status: str
    extract_status: str
    created_at: datetime


# ---------------------------------------------------------------- OCR
class OcrResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attachment_id: str
    engine: str
    status: str
    text_content: str | None = None
    page_count: int = 1
    text_length: int = 0
    avg_confidence: float | None = None
    duration_ms: int = 0
    error: str | None = None
    created_at: datetime

    @field_validator("text_content")
    @classmethod
    def _mask_ocr_text(cls, v: str | None) -> str | None:
        return mask_if_enabled(v)


class OcrResultDetail(OcrResultOut):
    blocks_json: list | None = None


# ---------------------------------------------------------------- 结构化记录
class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    message_id: str | None = None
    attachment_id: str | None = None
    room_id: str
    msgid: str | None = None
    template_id: str | None = None
    template_name: str | None = None
    # 模板定义的字段结构，用于详情页补全缺失字段（未抽取到的字段显示为空白行）
    fields_schema: list | None = None
    # 模板的展示样式与应用场景，随详情一并返回
    display_style: str | None = None
    scenario: str | None = None
    status: str
    fields_json: dict | None = None
    confidence: float | None = None
    model: str | None = None
    # 抽取路线：ocr（OCR 文本→文本 LLM）/ vision（多模态直抽兜底）
    extract_method: str | None = None
    # 抽取校验告警（行数缺失/金额不一致/置信度偏低等）
    extract_warnings: list | None = None
    duration_ms: int = 0
    error: str | None = None
    reviewed: bool = False
    review_note: str | None = None
    biz_time: datetime | None = None
    created_at: datetime

    @field_validator("fields_json")
    @classmethod
    def _mask_fields(cls, v: dict | None) -> dict | None:
        return _mask_value(v) if isinstance(v, dict) else v


class RecordUpdate(BaseModel):
    """人工复核：可直接修正抽错的字段"""

    fields_json: dict | None = None
    status: str | None = None
    reviewed: bool | None = None
    review_note: str | None = None


# ---------------------------------------------------------------- 模板
class FieldSpec(BaseModel):
    key: str
    label: str
    type: str = "string"  # string / number / date / array / object / boolean
    desc: str | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None = None
    enabled: bool = True
    priority: int = 0
    match_keywords: list | None = None
    match_file_exts: list | None = None
    fields_schema: list = Field(default_factory=list)
    prompt_extra: str | None = None
    is_fallback: bool = False
    # 展示样式与应用场景（详情页据此渲染不同版式）
    display_style: str = "card"
    scenario: str | None = None
    created_at: datetime
    updated_at: datetime


class TemplateCreate(BaseModel):
    name: str
    description: str | None = None
    enabled: bool = True
    priority: int = 0
    match_keywords: list[str] = Field(default_factory=list)
    match_file_exts: list[str] = Field(default_factory=list)
    fields_schema: list[FieldSpec] = Field(default_factory=list)
    prompt_extra: str | None = None
    is_fallback: bool = False
    display_style: str = "card"
    scenario: str | None = None


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    match_keywords: list[str] | None = None
    match_file_exts: list[str] | None = None
    fields_schema: list[FieldSpec] | None = None
    prompt_extra: str | None = None
    is_fallback: bool | None = None
    display_style: str | None = None
    scenario: str | None = None


class TemplateTryRun(BaseModel):
    """模板调试：直接贴一段文本试抽，不用真发文件"""

    text: str
    template_id: str | None = None


# ---------------------------------------------------------------- 群
class RoomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    room_id: str
    name: str | None = None
    owner: str | None = None
    member_count: int = 0
    msg_count: int = 0
    attachment_count: int = 0
    last_msg_at: datetime | None = None
    enabled: bool = True


class RoomUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------- 系统
class CursorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    seq: int
    total_fetched: int
    last_sync_at: datetime | None = None
    last_error: str | None = None
    updated_at: datetime


class HealthOut(BaseModel):
    status: str
    app: str
    collector_mode: str
    database: dict[str, Any]
    collector: dict[str, Any]
    ocr: dict[str, Any]
    llm: dict[str, Any]
    scheduler: dict[str, Any]


class ActionResult(BaseModel):
    ok: bool = True
    message: str = ""
    data: dict[str, Any] | None = None
