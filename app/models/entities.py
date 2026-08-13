"""
app/models/entities.py — 通用四层数据底座

分层设计（不锁死业务，后续加业务只加模板、不改表）：

    ① ChatMessage      原始消息层：群聊每条消息，保真存档（含 raw_json 兜底）
    ② Attachment       附件层    ：图片/文件/视频等媒体，落地到本地磁盘
    ③ OcrResult        识别层    ：OCR 抽出的纯文本 + 坐标块
    ④ ExtractedRecord  结构化层  ：按模板把文本抽成业务字段（JSON），可直接建视图供 BI

配套：
    ExtractTemplate    抽取模板：定义"什么文件用什么字段抽"，用户可在页面上配
    SyncCursor         同步游标：seq 断点续传，重启不丢数据
    ChatRoom           群档案  ：群名/人数统计

状态机（贯穿 ②③④，便于失败重试与可观测）：
    pending → processing → done / failed / skipped
"""
from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _uid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now()


# --------------------------------------------------------------------------
# ① 原始消息层
# --------------------------------------------------------------------------
class ChatMessage(Base):
    """企业微信会话存档的一条消息（解密后的明文）"""

    __tablename__ = "chat_message"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)

    # 存档序号，增量拉取的游标依据
    seq: Mapped[int] = mapped_column(BigInteger, index=True)
    # 企业微信消息唯一标识，用于去重（_external 结尾=外部消息）
    msgid: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    # send / recall / switch
    action: Mapped[str] = mapped_column(String(16), default="send")
    # text / image / file / video / voice / mixed / revoke / link / weapp ...
    msg_type: Mapped[str] = mapped_column(String(32), index=True)

    # 发送方：内部为 userid，外部为 external_userid（wo/wm 开头），机器人 wb 开头
    from_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    from_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_list: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # 群聊 id；单聊为空字符串
    room_id: Mapped[str] = mapped_column(String(128), index=True, default="")

    # 消息时间（企业微信给的是 ms 时间戳，这里同时留原值与可读时间）
    msg_time_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    msg_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # 文本类消息的正文；非文本类存摘要（如 [图片] xxx.png），便于全文检索
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 解密后的完整 JSON，保真兜底：新消息类型不改表也不丢数据
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 附件处理进度汇总，便于列表页直接展示
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)
    # 风险扫描标志：false=待扫，true=已扫（避免重复扫描）
    risk_scanned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_msg_room_time", "room_id", "msg_time"),
    )


# --------------------------------------------------------------------------
# ② 附件层
# --------------------------------------------------------------------------
class Attachment(Base):
    """消息里的媒体资源。sdkfileid 需通过 SDK 分片下载到本地"""

    __tablename__ = "attachment"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chat_message.id", ondelete="CASCADE"), index=True
    )
    # 冗余一份，方便不 join 直接按群统计
    room_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    msgid: Mapped[str] = mapped_column(String(128), index=True, default="")

    # image / file / video / voice / emotion
    media_type: Mapped[str] = mapped_column(String(32), index=True)
    sdkfileid: Mapped[str] = mapped_column(Text)

    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_ext: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    md5sum: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    local_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # pending / processing / done / failed / skipped
    download_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    download_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_retry: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # OCR 阶段状态（与下载状态分开，便于单独重跑）
    ocr_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 抽取阶段状态
    extract_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    message: Mapped["ChatMessage"] = relationship(back_populates="attachments")
    ocr_results: Mapped[list["OcrResult"]] = relationship(
        back_populates="attachment", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------
# ③ 识别层
# --------------------------------------------------------------------------
class OcrResult(Base):
    """一次 OCR 的产出。同一附件可能被多引擎/多次识别，故为一对多"""

    __tablename__ = "ocr_result"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    attachment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("attachment.id", ondelete="CASCADE"), index=True
    )

    engine: Mapped[str] = mapped_column(String(32), default="rapidocr")
    # done / failed
    status: Mapped[str] = mapped_column(String(16), default="done", index=True)

    # 拼接后的全文，供 LLM 抽取与全文检索
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 带坐标的原始块 [{box, text, score}]，供前端高亮定位
    blocks_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    page_count: Mapped[int] = mapped_column(Integer, default=1)
    text_length: Mapped[int] = mapped_column(Integer, default=0)
    avg_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    attachment: Mapped["Attachment"] = relationship(back_populates="ocr_results")


# --------------------------------------------------------------------------
# 抽取模板（业务可配置的核心）
# --------------------------------------------------------------------------
class ExtractTemplate(Base):
    """
    定义"什么样的文件，抽哪些字段"。

    匹配优先级：match_file_exts 与 match_keywords 命中即选，priority 大者优先。
    fields_schema 形如：
        [
          {"key": "invoice_no", "label": "发票号码", "type": "string", "desc": "8或20位数字"},
          {"key": "total_amount", "label": "价税合计", "type": "number"}
        ]
    """

    __tablename__ = "extract_template"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # 数值越大越优先；同分取先建
    priority: Mapped[int] = mapped_column(Integer, default=0)

    # OCR 文本命中任一关键词即匹配；空=不限
    match_keywords: Mapped[list | None] = mapped_column(JSON, default=list)
    # 文件扩展名白名单，如 [".jpg", ".pdf"]；空=不限
    match_file_exts: Mapped[list | None] = mapped_column(JSON, default=list)

    # 要抽取的字段定义
    fields_schema: Mapped[list] = mapped_column(JSON, default=list)
    # 追加到提示词末尾的业务说明（可选）
    prompt_extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 是否为兜底模板（都没命中时用它）
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --------------------------------------------------------------------------
# ④ 结构化层
# --------------------------------------------------------------------------
class ExtractedRecord(Base):
    """
    最终的业务基础数据表。

    fields_json 存放按模板抽出的键值对，可直接用 SQL/BI 展开成宽表：
      SQLite:     json_extract(fields_json, '$.invoice_no')
      PostgreSQL: fields_json ->> 'invoice_no'
    """

    __tablename__ = "extracted_record"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)

    message_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    attachment_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    room_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    msgid: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)

    template_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)

    # done / failed / need_review
    status: Mapped[str] = mapped_column(String(16), default="done", index=True)
    # 抽取出的业务字段
    fields_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 模型自评置信度 0~1
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 人工复核
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 业务时间：取自消息时间，便于按天聚合
    biz_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


# --------------------------------------------------------------------------
# 同步游标
# --------------------------------------------------------------------------
class SyncCursor(Base):
    """seq 断点续传。会话存档只保留最近 5 天，游标丢了就等于丢数据，必须持久化"""

    __tablename__ = "sync_cursor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, default="default")

    seq: Mapped[int] = mapped_column(BigInteger, default=0)
    total_fetched: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --------------------------------------------------------------------------
# 群档案
# --------------------------------------------------------------------------
class ChatRoom(Base):
    __tablename__ = "chat_room"

    room_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    # 群成员 userid 列表（逗号分隔），由 groupchat/get 同步回填
    members: Mapped[str] = mapped_column(Text, default="")

    msg_count: Mapped[int] = mapped_column(BigInteger, default=0)
    attachment_count: Mapped[int] = mapped_column(BigInteger, default=0)
    last_msg_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 是否纳入采集（配合 FILTER_ROOM_IDS 做白名单）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ExternalContact(Base):
    """外部联系人（客户）姓名缓存，由 externalcontact/get 解析回填。

    会话存档里发送方/群成员中的 external_userid（wo/wm 开头）本身不可读，
    通过本表缓存「企微客户联系」解析出的姓名，避免每次展示都打企微 API。
    """

    __tablename__ = "external_contact"

    external_userid: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 本企业对外部联系人的备注名 / 微信昵称
    name: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    avatar: Mapped[str | None] = mapped_column(String(512), nullable=True)
    corp_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gender: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WeComConfig(Base):
    """企业微信接口配置（单行，id 固定为 1）。

    界面化保存的唯一真相源；保存时同步写入内存 settings 并触发采集器重载，
    让 mock/archive 模式切换与凭证变更即时生效。
    """

    __tablename__ = "wecom_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # 运行模式：mock（演示）/ archive（真实会话存档）
    mode: Mapped[str] = mapped_column(String(16), default="mock")
    corp_id: Mapped[str] = mapped_column(String(128), default="")
    archive_secret: Mapped[str] = mapped_column(String(256), default="")
    # 客户联系 secret（外部群信息 externalcontact/groupchat/get 用，区别于存档 secret）
    customer_contact_secret: Mapped[str] = mapped_column(String(256), default="")
    # SDK 动态库路径（Windows .dll / Linux .so）
    sdk_path: Mapped[str] = mapped_column(String(512), default="")
    # RSA 私钥 PEM 内容（同时落盘到 private_key_path 供 SDK 解密读取）
    private_key_content: Mapped[str] = mapped_column(Text, default="")
    private_key_path: Mapped[str] = mapped_column(String(512), default="")
    proxy: Mapped[str] = mapped_column(String(256), default="")
    proxy_passwd: Mapped[str] = mapped_column(String(256), default="")
    sdk_timeout: Mapped[int] = mapped_column(Integer, default=30)
    # 单次拉取条数，官方上限 1000
    fetch_limit: Mapped[int] = mapped_column(Integer, default=500)
    # 企微应用（用于精准推送给具体人/部门，与群机器人 Webhook 互补）
    agent_id: Mapped[str] = mapped_column(String(128), default="")
    agent_secret: Mapped[str] = mapped_column(String(256), default="")
    # 是否只采集群聊（跳过单聊）
    only_group_chat: Mapped[bool] = mapped_column(Boolean, default=True)
    # 白名单群 roomid，逗号分隔，留空=全部
    filter_room_ids: Mapped[str] = mapped_column(Text, default="")
    # 企业微信 API 根地址（access_token / 群信息 / 成员列表等 HTTP 接口）
    api_base_url: Mapped[str] = mapped_column(String(256), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


__all__ = [
    "ChatMessage",
    "Attachment",
    "OcrResult",
    "ExtractTemplate",
    "ExtractedRecord",
    "SyncCursor",
    "ChatRoom",
    "WeComConfig",
]
