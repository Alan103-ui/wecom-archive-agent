"""
app/api/models.py — 模型连接配置接口（通用化：本地 Ollama + 外部 OpenAI 兼容）

路由前缀 /models（在 api/__init__.py 注册）。
覆盖：
  GET    /models              列表（api_key 脱敏）
  POST   /models              新建
  GET    /models/{id}         详情
  PATCH  /models/{id}         更新
  DELETE /models/{id}         删除
  POST   /models/{id}/test    连通性 + 样例 JSON 自检
  POST   /models/fetch-models 不落库，按 provider/base_url/api_key 拉取远端模型清单
  GET    /models/roles        角色 → 当前生效配置的绑定关系
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.model_config import (
    PROVIDERS,
    ROLES,
    ROLE_EXTRACT_VISION,
    ROLE_LABELS,
    ModelConfig,
)
from app.services.llm import client as llm_client
from app.services.llm.client import _normalize_base_url

router = APIRouter()


# ---------------------------------------------------------------- 入参 / 出参
class ModelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    provider: str = Field(..., pattern="^(ollama|openai)$")
    base_url: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=4096)
    model: str = Field(default="", max_length=128)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    timeout: int = Field(default=180, ge=5, le=600)
    enabled: bool = True
    is_default: bool = False
    roles: list[str] = Field(default_factory=list)


class ModelUpdate(BaseModel):
    name: str | None = None
    provider: str | None = Field(default=None, pattern="^(ollama|openai)$")
    base_url: str | None = None
    # api_key 留空 = 不修改；传非空 = 覆盖
    api_key: str | None = Field(default=None, max_length=4096)
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    timeout: int | None = Field(default=None, ge=5, le=600)
    enabled: bool | None = None
    is_default: bool | None = None
    roles: list[str] | None = None


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    provider: str
    base_url: str
    api_key_set: bool = False  # 不回传明文
    model: str
    temperature: float
    timeout: int
    enabled: bool
    is_default: bool
    roles: list[str]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActionResult(BaseModel):
    id: str | None = None
    message: str
    data: dict = Field(default_factory=dict)


class FetchModelsIn(BaseModel):
    provider: str = Field(..., pattern="^(ollama|openai)$")
    base_url: str
    api_key: str = ""
    config_id: str | None = None  # 编辑已有连接时，旧 key 留空则回退取已存 key


class ProbeIn(BaseModel):
    provider: str = Field(..., pattern="^(ollama|openai)$")
    base_url: str
    api_key: str = ""
    model: str = ""
    config_id: str | None = None


# ---------------------------------------------------------------- 工具
def _slug(name: str, db: Session) -> str:
    base = re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", "-", name.lower()).strip("-") or "model"
    slug = base
    i = 1
    while db.get(ModelConfig, slug):
        slug = f"{base}-{i}"
        i += 1
    return slug


def _to_out(c: ModelConfig) -> ModelOut:
    return ModelOut(
        id=c.id,
        name=c.name,
        provider=c.provider,
        base_url=c.base_url,
        api_key_set=bool(c.api_key),
        model=c.model,
        temperature=c.temperature,
        timeout=c.timeout,
        enabled=c.enabled,
        is_default=c.is_default,
        roles=list(c.roles or []),
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


# ---------------------------------------------------------------- 路由
@router.get("", response_model=list[ModelOut], summary="模型连接列表")
def list_models(db: Session = Depends(get_db)):
    rows = db.query(ModelConfig).order_by(ModelConfig.created_at).all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=ActionResult, summary="新建模型连接")
def create_model(body: ModelCreate, db: Session = Depends(get_db)):
    roles = [r for r in body.roles if r in ROLES]
    cid = _slug(body.name, db)
    cfg = ModelConfig(
        id=cid,
        name=body.name,
        provider=body.provider,
        base_url=_normalize_base_url(body.base_url),
        api_key=body.api_key,
        model=body.model,
        temperature=body.temperature,
        timeout=body.timeout,
        enabled=body.enabled,
        is_default=body.is_default,
        roles=roles,
    )
    if body.is_default:
        _clear_other_defaults(db, cid)
    db.add(cfg)
    db.commit()
    return ActionResult(id=cid, message="已创建模型连接", data={"config": _to_out(cfg).model_dump(mode="json")})


@router.get("/roles", summary="角色→配置 绑定关系")
def role_bindings(db: Session = Depends(get_db)):
    configs = db.query(ModelConfig).order_by(ModelConfig.created_at).all()
    binding: dict[str, dict] = {}
    for role in ROLES:
        served = None
        for c in configs:
            if c.enabled and role in (c.roles or []):
                served = {"id": c.id, "name": c.name}
                break
        # 视觉(多模态)用途不允许回退到文本模型兜底：无人显式勾选即视为未绑定
        if served is None and role == ROLE_EXTRACT_VISION:
            binding[role] = served
            continue
        if served is None:
            dft = next((c for c in configs if c.enabled and c.is_default), None)
            if dft:
                served = {"id": dft.id, "name": dft.name, "via_default": True}
        binding[role] = served
    return {
        "roles": [
            {"role": r, "label": ROLE_LABELS.get(r, r), "served_by": binding.get(r)}
            for r in ROLES
        ],
        "configs": [_to_out(c).model_dump(mode="json") for c in configs],
    }


@router.get("/{model_id}", response_model=ModelOut, summary="模型连接详情")
def get_model(model_id: str, db: Session = Depends(get_db)):
    c = db.get(ModelConfig, model_id)
    if not c:
        raise HTTPException(404, "模型连接不存在")
    return _to_out(c)


@router.patch("/{model_id}", response_model=ActionResult, summary="更新模型连接")
def update_model(model_id: str, body: ModelUpdate, db: Session = Depends(get_db)):
    c = db.get(ModelConfig, model_id)
    if not c:
        raise HTTPException(404, "模型连接不存在")

    if body.name is not None:
        c.name = body.name
    if body.provider is not None:
        if body.provider not in PROVIDERS:
            raise HTTPException(400, "provider 必须为 ollama / openai")
        c.provider = body.provider
    if body.base_url is not None:
        c.base_url = _normalize_base_url(body.base_url)
    if body.api_key is not None:
        # 空字符串 = 不修改，保留原 key
        if body.api_key != "":
            c.api_key = body.api_key
    if body.model is not None:
        c.model = body.model
    if body.temperature is not None:
        c.temperature = body.temperature
    if body.timeout is not None:
        c.timeout = body.timeout
    if body.enabled is not None:
        c.enabled = body.enabled
    if body.roles is not None:
        c.roles = [r for r in body.roles if r in ROLES]
    if body.is_default is not None:
        c.is_default = body.is_default
        if body.is_default:
            _clear_other_defaults(db, model_id)

    c.updated_at = datetime.now(timezone.utc)
    db.commit()
    llm_client._ROLE_CACHE.clear()  # 路由缓存失效
    return ActionResult(id=model_id, message="已更新", data={"config": _to_out(c).model_dump(mode="json")})


@router.delete("/{model_id}", response_model=ActionResult, summary="删除模型连接")
def delete_model(model_id: str, db: Session = Depends(get_db)):
    c = db.get(ModelConfig, model_id)
    if not c:
        raise HTTPException(404, "模型连接不存在")
    db.delete(c)
    db.commit()
    llm_client._ROLE_CACHE.clear()
    return ActionResult(id=model_id, message="已删除模型连接")


@router.post("/{model_id}/test", response_model=ActionResult, summary="连通性 + 样例自检")
def test_model(model_id: str, db: Session = Depends(get_db)):
    c = db.get(ModelConfig, model_id)
    if not c:
        raise HTTPException(404, "模型连接不存在")
    result = llm_client.test_model(c)
    return ActionResult(id=model_id, message="自检完成", data=result)


@router.post("/fetch-models", summary="按连接信息拉取远端模型清单（不落库）")
def fetch_models(body: FetchModelsIn, db: Session = Depends(get_db)):
    api_key = body.api_key
    if not api_key and body.config_id:
        existing = db.get(ModelConfig, body.config_id)
        if existing:
            api_key = existing.api_key
    fake = ModelConfig(
        id="_temp",
        name="临时",
        provider=body.provider,
        base_url=_normalize_base_url(body.base_url),
        api_key=api_key,
        model="",
    )
    try:
        models = llm_client.list_remote_models(fake)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"拉取失败：{e}")
    return {"provider": body.provider, "base_url": _normalize_base_url(body.base_url), "models": models}


# ---------------------------------------------------------------- 内部
@router.post("/probe", response_model=ActionResult, summary="对未保存的连接做连通性+样例自检")
def probe_model(body: ProbeIn, db: Session = Depends(get_db)):
    api_key = body.api_key
    # 编辑已有连接时旧 key 留空 → 回退取已存 key（避免重新粘贴整串密钥）
    if not api_key and body.config_id:
        existing = db.get(ModelConfig, body.config_id)
        if existing:
            api_key = existing.api_key
    fake = ModelConfig(
        id="_probe",
        name="探针",
        provider=body.provider,
        base_url=_normalize_base_url(body.base_url),
        api_key=api_key,
        model=body.model,
    )
    result = llm_client.test_model(fake)
    return ActionResult(message="自检完成", data=result)


# ---------------------------------------------------------------- 内部
def _clear_other_defaults(db: Session, keep_id: str) -> None:
    db.execute(
        select(ModelConfig).where(ModelConfig.id != keep_id, ModelConfig.is_default == True)
    )
    for c in db.query(ModelConfig).filter(ModelConfig.id != keep_id, ModelConfig.is_default == True).all():
        c.is_default = False
