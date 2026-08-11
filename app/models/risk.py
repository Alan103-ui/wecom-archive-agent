"""
app/models/risk.py — 风险研判与分级预警数据模型

围绕"客户/采购沟通群"场景：
    RiskRule      风险规则：关键词+LLM 双引擎的判定依据，按群(room)路由到管理层
    AlertLayer    管理层级：业务主管层 / 部门总监层 / 总经理·合规层
    AlertTarget   投递目标：企微群机器人 Webhook / 应用消息 / 邮件 / 系统内
    RiskEvent     风险事件：某条消息命中某个风险分类，触发预警后落库
    AlertLog      投递回执：每次发送一条记录，失败可重发

设计要点：
    - 通用分类（价格异常/私下交易/回扣/竞品撬单/客户投诉/信息泄露/合规）不锁死，
      用户可在页面增删规则，不动代码。
    - 路由靠 rule.scope_rooms(空=全群) + rule.alert_layers(空=按严重度兜底) 实现
      "不同群 → 不同管理层"。
"""
from __future__ import annotations

from datetime import datetime

import uuid
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
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
# 风险规则
# --------------------------------------------------------------------------
class RiskRule(Base):
    """风险判定规则：关键词规则 + LLM 语义，按群路由到管理层"""

    __tablename__ = "risk_rule"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 风险分类（取自 app.services.risk.categories 的固定枚举）
    category: Mapped[str] = mapped_column(String(64), index=True)

    # low / medium / high / critical
    severity: Mapped[str] = mapped_column(String(16), default="medium", index=True)

    # 作用群范围：room_id 列表，空=所有群
    scope_rooms: Mapped[list] = mapped_column(JSON, default=list)

    # 关键词引擎：正则字符串列表（命中任一即触发）
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    # 追加给 LLM 的判定提示（可选）
    llm_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 命中点路由到哪些管理层（AlertLayer.id 列表）；空=按 severity 兜底
    alert_layers: Mapped[list] = mapped_column(JSON, default=list)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --------------------------------------------------------------------------
# 管理层级
# --------------------------------------------------------------------------
class AlertLayer(Base):
    """管理层级。level 越小越基层（先触达），越大越高管"""

    __tablename__ = "alert_layer"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    level: Mapped[int] = mapped_column(Integer, default=1, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------
# 投递目标
# --------------------------------------------------------------------------
class AlertTarget(Base):
    """某管理层在某通道上的投递目标"""

    __tablename__ = "alert_target"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    layer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("alert_layer.id", ondelete="CASCADE"), index=True
    )
    # webhook / app / email / system
    channel: Mapped[str] = mapped_column(String(16), index=True)
    # webhook URL / 应用消息的 userid 或 "party:xxx" / 邮箱地址
    target: Mapped[str] = mapped_column(String(512), default="")
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    layer: Mapped["AlertLayer"] = relationship("AlertLayer")


# --------------------------------------------------------------------------
# 风险事件
# --------------------------------------------------------------------------
class RiskEvent(Base):
    """一条消息命中某个风险分类后生成的事件，触发预警并落库"""

    __tablename__ = "risk_event"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    message_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("chat_message.id", ondelete="SET NULL"), index=True, nullable=True
    )
    room_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    from_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)

    rule_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="medium", index=True)
    # keyword / llm
    detection_method: Mapped[str] = mapped_column(String(16), default="keyword", index=True)
    matched_keyword: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # 命中的原文片段，供人工复核与告警展示
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    # LLM 给出的判定理由
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # pending / acknowledged / resolved / ignored
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # unsent / partial / sent / failed
    alert_status: Mapped[str] = mapped_column(String(16), default="unsent", index=True)

    biz_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    alert_logs: Mapped[list["AlertLog"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("message_id", "category", name="uq_risk_event_msg_cat"),
    )


# --------------------------------------------------------------------------
# 投递回执
# --------------------------------------------------------------------------
class AlertLog(Base):
    """每次预警投递一条记录，便于审计与失败重发"""

    __tablename__ = "alert_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uid)
    event_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("risk_event.id", ondelete="CASCADE"), index=True
    )
    layer_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    target: Mapped[str] = mapped_column(String(512), default="")
    # sent / failed / skipped
    status: Mapped[str] = mapped_column(String(16), default="sent", index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    event: Mapped["RiskEvent"] = relationship(back_populates="alert_logs")


__all__ = [
    "RiskRule",
    "AlertLayer",
    "AlertTarget",
    "RiskEvent",
    "AlertLog",
]
