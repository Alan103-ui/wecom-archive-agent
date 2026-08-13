"""
app/services/contact_resolver.py — 外部联系人姓名解析

把会话存档里的 external_userid（wo/wm 开头）解析成可读姓名，用于：
  - 消息发送人展示（ChatMessage.from_name）
  - 群主 / 外部群成员展示

解析优先级（全部 best-effort，失败不影响存档主流程）：
  1. 进程内内存缓存（TTL，最快，重启失效）
  2. 数据库缓存表 external_contact（跨重启持久化）
  3. 企微 externalcontact/get 实时拉取（仅未命中时，按 id 逐个，受速率限制）

⚠️ caller-specific ID 注意：同一外部联系人的 external_userid 在不同调用方
（企业 / 第三方服务商）取值不同。本解析只能用「本企业」客户联系 secret 换取的
token 去查，与会话存档存档方保持一致，否则对不上。
"""
from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.models.entities import ExternalContact
from app.services import wecom_api

logger = logging.getLogger(__name__)

# 进程内缓存：external_userid -> (name, 写入时间戳)
_MEM: dict[str, tuple[str, float]] = {}
_TTL = 86400 * 7  # 7 天，外部联系人姓名极少变动


def _is_external(uid: str) -> bool:
    """外部联系人 userid 以 wo / wm 开头（企业成员 / 机器人不在此列）。"""
    return bool(uid) and uid[:2] in ("wo", "wm")


def resolve_names(db: Session, ids: list[str]) -> dict[str, str]:
    """批量解析一批 userid 中的外部联系人为 {userid: name}。

    - 非外部 id（员工 / 机器人）直接跳过，不出现在返回里。
    - 解析失败的 id 也不出现（调用方保留原始 id 展示）。
    - 实时拉取到的姓名会写回 _MEM 与 external_contact 表（由调用方提交事务）。
    """
    result: dict[str, str] = {}
    now = time.time()
    to_fetch: list[str] = []

    for uid in ids or []:
        if not _is_external(uid):
            continue
        cached = _MEM.get(uid)
        if cached and (now - cached[1]) < _TTL:
            result[uid] = cached[0]
            continue
        row = db.get(ExternalContact, uid)
        if row and row.name:
            result[uid] = row.name
            _MEM[uid] = (row.name, now)
            continue
        to_fetch.append(uid)

    for uid in to_fetch:
        name = ""
        info = None
        try:
            info = wecom_api.get_external_contact(uid)
            name = (info.get("name") or "").strip()
        except Exception as e:  # noqa: BLE001  best-effort：解析失败不阻断主流程
            logger.warning("解析外部联系人姓名失败 %s: %s", uid, e)
        if name:
            result[uid] = name
            _MEM[uid] = (name, now)
            _persist(db, uid, name, info)

    # 注意：本函数不在此处 commit。ExternalContact 的 upsert 随调用方事务一并提交，
    # 避免误滚调用方尚未提交的其它写入（如正在入库的 ChatMessage）。
    # - 存档流程：pipeline 既有 commit 会一并提交。
    # - 端点场景：/api/wecom/external-contacts 调用方需自行 commit。
    return result


def _persist(db: Session, uid: str, name: str, info: dict | None) -> None:
    row = db.get(ExternalContact, uid)
    if row is None:
        row = ExternalContact(external_userid=uid)
        db.add(row)
    row.name = name
    if info:
        row.corp_name = info.get("corp_name")
        row.avatar = info.get("avatar")
        row.gender = info.get("gender")
        row.type = info.get("type")
        row.raw_json = info
