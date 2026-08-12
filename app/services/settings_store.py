"""
app/services/settings_store.py — 标准化的运行期配置读取层

解决两类"界面配了但不生效 / 重启丢失"的问题：
  1. SMTP 邮件：原来只读 env，没有界面；改为 KV 存储，界面可配、即时生效、重启安全。
  2. 企微应用消息推送凭证：原来只同步到内存 settings（重启即丢）；改为运行期直接读
     wecom_config 单行(id=1)，DB 持久化，重启后仍生效。

设计原则（面向标准化，而非定制）：
  - 所有"运行期可变"的配置都走这里，sender / token 不再直接读 env settings。
  - KV 优先、env 兜底；wecom_config 持久化、env 兜底。
  - 不在此处写任何业务分支，纯粹做"取/存"。
"""
from __future__ import annotations

from app.config import settings
from app.db.database import SessionLocal
from app.models.entities import WeComConfig


# ---------------------------------------------------------------- SMTP（KV 存储）
def get_smtp_config() -> dict:
    """运行期读取 SMTP 配置：KV 优先，兜底 env/settings。"""
    from app.services.kv_store import get_setting

    def _kv(key, default):
        v = get_setting(key, None)
        return default if v is None else v

    return {
        "host": _kv("SMTP_HOST", settings.SMTP_HOST),
        "port": int(_kv("SMTP_PORT", settings.SMTP_PORT) or 465),
        "user": _kv("SMTP_USER", settings.SMTP_USER),
        "pass": _kv("SMTP_PASS", settings.SMTP_PASS),
        "from": _kv("SMTP_FROM", settings.SMTP_FROM),
        "tls": bool(_kv("SMTP_TLS", settings.SMTP_TLS)),
    }


def set_smtp_config(cfg: dict) -> None:
    """写入 SMTP 配置到 KV。pass 留空表示保留原值。"""
    from app.services.kv_store import set_setting

    cur = get_smtp_config()
    set_setting("SMTP_HOST", cfg.get("host", "") or "")
    set_setting("SMTP_PORT", int(cfg.get("port") or 465))
    set_setting("SMTP_USER", cfg.get("user", "") or "")
    # 密码：仅当用户显式填写才更新，留空保留原值
    set_setting("SMTP_PASS", cfg.get("pass") or cur["pass"] or "")
    set_setting("SMTP_FROM", cfg.get("from", "") or "")
    set_setting("SMTP_TLS", bool(cfg.get("tls", True)))


# ---------------------------------------------------------------- 企微应用消息（DB 单行）
def get_wecom_app_config() -> dict:
    """运行期读取企微应用推送凭证：wecom_config 单行优先，兜底 env/settings。

    返回 corp_id / agent_id / agent_secret / api_base_url。
    """
    db = SessionLocal()
    try:
        row = db.get(WeComConfig, 1)
    finally:
        db.close()
    if row is not None and (row.corp_id or row.agent_id or row.agent_secret):
        return {
            "corp_id": row.corp_id or settings.WECOM_CORP_ID,
            "agent_id": row.agent_id or settings.WECOM_AGENT_ID,
            "agent_secret": row.agent_secret or settings.WECOM_AGENT_SECRET,
            "api_base_url": row.api_base_url or settings.WECOM_API_BASE_URL,
        }
    return {
        "corp_id": settings.WECOM_CORP_ID,
        "agent_id": settings.WECOM_AGENT_ID,
        "agent_secret": settings.WECOM_AGENT_SECRET,
        "api_base_url": settings.WECOM_API_BASE_URL,
    }


def set_wecom_app_config(cfg: dict) -> None:
    """写入企微应用推送凭证到 wecom_config 单行。agent_secret 留空保留原值。"""
    db = SessionLocal()
    try:
        row = db.get(WeComConfig, 1)
        if row is None:
            row = WeComConfig(id=1)
            db.add(row)
        cur = get_wecom_app_config()
        row.corp_id = cfg.get("corp_id", "") or ""
        row.agent_id = cfg.get("agent_id", "") or ""
        row.agent_secret = cfg.get("agent_secret") or cur["agent_secret"] or ""
        if not row.api_base_url:
            row.api_base_url = settings.WECOM_API_BASE_URL
        db.commit()
    finally:
        db.close()
