"""
app/api/wecom_config.py — 企业微信接口配置（界面化）

配置页「企业微信」子 TAB 的后端：
  GET  /api/wecom-config   读取当前配置（DB 单行，无则 fallback 到 settings/env）
  PUT  /api/wecom-config   保存并热生效（写库 + 同步内存 settings + 重载采集器）

设计要点：
  - WeComConfig 是单行表（id=1），作为界面保存的唯一真相源。
  - 私钥内容同时落盘到 data/sdk/private_key.pem，复用 crypto.py 现有文件读取逻辑，零回归。
  - 保存后把字段同步进内存 settings 并 reset_collector()，让 mock/archive 切换与
    凭证变更即时生效，无需重启进程。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.collectors import get_collector, reset_collector
from app.config import BASE_DIR, settings
from app.db.database import get_db
from app.models.entities import WeComConfig
from app.services.wecom_token import verify_token

router = APIRouter()

_DEFAULT_KEY_PATH = BASE_DIR / "data" / "sdk" / "private_key.pem"


class WeComConfigIn(BaseModel):
    mode: str = Field("mock", description="mock=演示 / archive=真实会话存档")
    corp_id: str = ""
    archive_secret: str = ""
    sdk_path: str = ""
    private_key_content: str = Field("", description="RSA 私钥 PEM 内容；留空=不修改已存私钥")
    private_key_path: str = ""
    proxy: str = ""
    proxy_passwd: str = ""
    sdk_timeout: int = 30
    fetch_limit: int = 500
    agent_id: str = ""
    agent_secret: str = ""
    only_group_chat: bool = True
    filter_room_ids: str = ""
    api_base_url: str = ""


class WeComVerifyIn(BaseModel):
    corp_id: str = ""
    agent_id: str = ""  # 占位，取 token 仅需 corpid + secret
    agent_secret: str = ""        # 应用消息推送 secret（告警通道用）
    archive_secret: str = ""      # 会话内容存档 secret（archive 模式真正依赖）
    api_base_url: str = ""


# ---------------------------------------------------------------- 读取
@router.get("/wecom-config", summary="读取企业微信接口配置")
def get_wecom_config(db: Session = Depends(get_db)):
    row = db.get(WeComConfig, 1)
    if row is None:
        # 尚未保存过：用当前 env/settings 作为默认值回填
        key_path = settings.WECOM_PRIVATE_KEY_PATH
        existing_key = ""
        if key_path and Path(key_path).exists():
            try:
                existing_key = Path(key_path).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                existing_key = ""
        return {
            "id": 1,
            "mode": settings.COLLECTOR_MODE,
            "corp_id": settings.WECOM_CORP_ID,
            "archive_secret": settings.WECOM_ARCHIVE_SECRET,
            "sdk_path": settings.WECOM_SDK_PATH,
            "private_key_content": existing_key,
            "private_key_path": key_path,
            "proxy": settings.WECOM_PROXY,
            "proxy_passwd": settings.WECOM_PROXY_PASSWD,
            "sdk_timeout": settings.WECOM_SDK_TIMEOUT,
            "fetch_limit": settings.WECOM_FETCH_LIMIT,
            "agent_id": settings.WECOM_AGENT_ID,
            "agent_secret": settings.WECOM_AGENT_SECRET,
            "only_group_chat": settings.ONLY_GROUP_CHAT,
            "filter_room_ids": settings.FILTER_ROOM_IDS,
            "api_base_url": settings.WECOM_API_BASE_URL,
            "updated_at": None,
            "source": "env",  # 提示前端：当前值来自环境变量，尚未页面保存
        }

    return {
        "id": row.id,
        "mode": row.mode,
        "corp_id": row.corp_id,
        "archive_secret": row.archive_secret,
        "sdk_path": row.sdk_path,
        "private_key_content": row.private_key_content,
        "private_key_path": row.private_key_path,
        "proxy": row.proxy,
        "proxy_passwd": row.proxy_passwd,
        "sdk_timeout": row.sdk_timeout,
        "fetch_limit": row.fetch_limit,
        "agent_id": row.agent_id,
        "agent_secret": row.agent_secret,
        "only_group_chat": row.only_group_chat,
        "filter_room_ids": row.filter_room_ids,
        "api_base_url": row.api_base_url or settings.WECOM_API_BASE_URL,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "source": "db",
    }


# ---------------------------------------------------------------- 验证连通性
@router.post("/wecom-config/verify", summary="验证企业微信凭证连通性（真实请求一次 gettoken）")
def post_verify_wecom(payload: WeComVerifyIn):
    """用 corpid + secret 真实请求一次 access_token，返回官方 errcode/errmsg。

    不落库，仅用于配置页「验证连通性」按钮，让用户在保存前就能确认凭证有效。
    archive 模式真正拉取会话存档依赖「会话内容存档应用 secret」(archive_secret)，
    因此优先验证它；未填写时 fallback 到应用 secret(agent_secret) 以兼容简化部署。
    """
    base = payload.api_base_url.strip() or settings.WECOM_API_BASE_URL
    secret = (payload.archive_secret or payload.agent_secret).strip()
    result = verify_token(payload.corp_id.strip(), secret, base)
    return result


# ---------------------------------------------------------------- 保存
@router.put("/wecom-config", summary="保存企业微信接口配置并热生效")
def put_wecom_config(payload: WeComConfigIn, db: Session = Depends(get_db)):
    if payload.mode not in ("mock", "archive"):
        raise HTTPException(400, "mode 只能是 mock 或 archive")
    if payload.sdk_timeout <= 0:
        raise HTTPException(400, "SDK 超时必须为正整数")
    if not (1 <= payload.fetch_limit <= 1000):
        raise HTTPException(400, "单次拉取条数需在 1-1000 之间")

    # 私钥落盘：若提供了新内容，写到 private_key_path（未填则用默认路径）
    key_path = payload.private_key_path.strip() or str(_DEFAULT_KEY_PATH)
    key_written = False
    if payload.private_key_content and payload.private_key_content.strip():
        p = Path(key_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(payload.private_key_content, encoding="utf-8")
        key_written = True

    # 写库（upsert 单行）
    row = db.get(WeComConfig, 1)
    if row is None:
        row = WeComConfig(id=1)
        db.add(row)

    row.mode = payload.mode
    row.corp_id = payload.corp_id.strip()
    row.archive_secret = payload.archive_secret.strip()
    row.sdk_path = payload.sdk_path.strip() or str(BASE_DIR / "data" / "sdk" / "WeWorkFinanceSdk.dll")
    if key_written:
        row.private_key_content = payload.private_key_content
        row.private_key_path = key_path
    else:
        # 未填写新私钥：保留原私钥内容；路径缺失时补默认
        if not row.private_key_path:
            row.private_key_path = key_path
    row.proxy = payload.proxy.strip()
    row.proxy_passwd = payload.proxy_passwd.strip()
    row.sdk_timeout = payload.sdk_timeout
    row.fetch_limit = payload.fetch_limit
    row.agent_id = payload.agent_id.strip()
    row.agent_secret = payload.agent_secret.strip()
    row.only_group_chat = payload.only_group_chat
    row.filter_room_ids = payload.filter_room_ids.strip()
    row.api_base_url = payload.api_base_url.strip() or settings.WECOM_API_BASE_URL
    db.commit()
    db.refresh(row)

    # 同步到内存 settings，让运行时立即采用新值
    settings.COLLECTOR_MODE = row.mode
    settings.WECOM_CORP_ID = row.corp_id
    settings.WECOM_ARCHIVE_SECRET = row.archive_secret
    settings.WECOM_SDK_PATH = row.sdk_path
    settings.WECOM_PRIVATE_KEY_PATH = row.private_key_path
    settings.WECOM_PRIVATE_KEY_MAP = {}  # 单私钥模式；多版本场景仍走 .env
    settings.WECOM_PROXY = row.proxy
    settings.WECOM_PROXY_PASSWD = row.proxy_passwd
    settings.WECOM_SDK_TIMEOUT = row.sdk_timeout
    settings.WECOM_FETCH_LIMIT = row.fetch_limit
    settings.WECOM_AGENT_ID = row.agent_id
    settings.WECOM_AGENT_SECRET = row.agent_secret
    settings.ONLY_GROUP_CHAT = row.only_group_chat
    settings.FILTER_ROOM_IDS = row.filter_room_ids
    settings.WECOM_API_BASE_URL = row.api_base_url

    # 重载采集器，使 mock/archive 切换与凭证变更即时生效
    reset_collector()
    health = None
    try:
        ok, detail = get_collector().health_check()
        health = {"ok": bool(ok), "detail": detail}
    except Exception as e:  # noqa: BLE001
        health = {"ok": False, "detail": str(e)[:300]}

    return {
        "ok": True,
        "message": "已保存并重新加载采集器",
        "mode": row.mode,
        "key_written": key_written,
        "health": health,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
