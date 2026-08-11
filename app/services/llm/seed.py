"""
app/services/llm/seed.py — 模型连接默认种子

首次启动（库里没有任何 ModelConfig）时，按 .env 里的 OLLAMA_* 播种一个
「本地 Ollama」连接，并同时服务于 结构化抽取(extract) 与 风险研判(risk)，
保证开箱即用、行为与改造前一致。
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.config import settings
from app.models.model_config import (
    PROVIDER_OLLAMA,
    ROLE_EXTRACT,
    ROLE_RISK,
    ModelConfig,
)


def seed_model_defaults(db) -> None:
    if db.execute(select(func.count()).select_from(ModelConfig)).scalar() > 0:
        return
    cfg = ModelConfig(
        id="local-ollama",
        name="本地 Ollama",
        provider=PROVIDER_OLLAMA,
        base_url=settings.OLLAMA_BASE_URL,
        api_key="",
        model=settings.OLLAMA_MODEL,
        temperature=settings.OLLAMA_TEMPERATURE,
        timeout=settings.OLLAMA_TIMEOUT,
        enabled=True,
        is_default=True,
        roles=[ROLE_EXTRACT, ROLE_RISK],
    )
    db.add(cfg)
    db.commit()
