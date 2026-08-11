"""
app/models/kv.py — 通用键值设置（前端可改的运行期配置持久化）

用于保存"不需要重启即可生效"的配置项，例如超时回复提醒的开关/阈值/严重度。
与 pydantic-settings（.env，启动期生效）互补：这里存的值覆盖 .env 默认值。
"""
from __future__ import annotations

from datetime import datetime

import uuid
from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _now() -> datetime:
    return datetime.now()


def _uid() -> str:
    return uuid.uuid4().hex


class KVSetting(Base):
    """任意 JSON 值的持久化（key 唯一）。value_json 存 dict/list/scalar 均可。"""

    __tablename__ = "kv_setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    __table_args__ = ()


__all__ = ["KVSetting"]
