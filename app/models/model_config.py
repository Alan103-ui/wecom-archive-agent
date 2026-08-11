"""
app/models/model_config.py — 模型连接配置（通用化、界面可配）

设计目标：
- 把"用哪个模型"从硬编码（config.py 的 OLLAMA_*）下沉到数据库，界面可增删改。
- 统一抽象两类提供方：
    - ollama : 本地模型，走原生 /api/chat（支持 format=json 强约束）
    - openai : 一切 OpenAI 兼容端点（OpenAI / DeepSeek / 通义 / vLLM / Azure 等），
               走 /v1/chat/completions + Bearer 鉴权 + response_format=json_object
- roles 字段声明该连接"服务于哪些用途"，目前固定两类：
    - extract : 附件 OCR 文本的结构化抽取
    - risk    : 风险研判的 LLM 语义引擎
  调度/抽取/风险在运行时按 role 查询"启用且 roles 包含该 role"的连接，
  找不到则退而求其次用 is_default 连接。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base

# 支持的提供方类型
PROVIDER_OLLAMA = "ollama"
PROVIDER_OPENAI = "openai"
PROVIDERS = [PROVIDER_OLLAMA, PROVIDER_OPENAI]

# 模型用途（角色）。新增用途只需在这里加 key，并在前端勾选项里加一项。
ROLE_EXTRACT = "extract"
ROLE_RISK = "risk"
ROLE_EXTRACT_VISION = "extract_vision"
ROLES = [ROLE_EXTRACT, ROLE_RISK, ROLE_EXTRACT_VISION]
ROLE_LABELS = {
    ROLE_EXTRACT: "结构化抽取",
    ROLE_RISK: "风险研判",
    ROLE_EXTRACT_VISION: "视觉抽取(多模态)",
}


class ModelConfig(Base):
    __tablename__ = "model_config"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # 稳定 slug
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), default=PROVIDER_OLLAMA)  # ollama | openai
    base_url: Mapped[str] = mapped_column(String(512), default="")
    # api_key 可为空（本地 Ollama 通常不需要）；API 返回时脱敏，绝不回传明文
    api_key: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    temperature: Mapped[float] = mapped_column(Float, default=0.1)
    timeout: Mapped[int] = mapped_column(Integer, default=180)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 该连接服务的角色列表，如 ["extract", "risk"]
    roles: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
