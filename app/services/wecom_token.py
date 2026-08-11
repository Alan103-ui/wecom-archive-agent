"""
app/services/wecom_token.py — 企业微信 access_token 统一缓存与校验

企业微信所有辅助 HTTP 接口（群信息 groupchat/get、开启存档成员 getpermituserlist、
应用消息推送等）都必须携带 access_token，而 access_token 由 corpid + corpsecret 换取。

官方硬性要求（doc 10013）：
  - 必须缓存，不能频繁调 gettoken（否则被频率拦截）
  - 有效期 expires_in（正常 7200s），提前刷新
  - 至少预留 512 字节存储
  - 每个应用的 token 独立缓存（按 corpid+secret 区分）
  - 企微可能提前失效，需实现失效重取

本模块取代原先散落在 alert/sender.py 里的私有 _TOKEN 缓存，供全项目统一复用。
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# key = (corpid, corpsecret) -> {"value": token, "exp": 过期 unix 秒}
_TOKEN_CACHE: dict = {}
_LOCK = threading.Lock()

_DEFAULT_BASE = "https://qyapi.weixin.qq.com"


def _base(base_url: str | None) -> str:
    return (base_url or settings.WECOM_API_BASE_URL or _DEFAULT_BASE).rstrip("/")


def get_access_token(
    corpid: str,
    corpsecret: str,
    base_url: str | None = None,
    force: bool = False,
) -> str | None:
    """取 access_token，命中缓存则直接返回，否则请求 gettoken 并缓存。

    返回 None 表示凭证为空或换取失败（调用方应降级处理）。
    """
    if not corpid or not corpsecret:
        return None
    base = _base(base_url)
    key = (corpid, corpsecret, base)
    now = time.time()
    if not force:
        with _LOCK:
            cached = _TOKEN_CACHE.get(key)
            if cached and cached["exp"] > now + 60:  # 提前 60s 刷新
                return cached["value"]
    try:
        with httpx.Client(timeout=10, proxy=settings.WECOM_PROXY or None) as client:
            r = client.get(
                f"{_base(base_url)}/cgi-bin/gettoken",
                params={"corpid": corpid, "corpsecret": corpsecret},
            )
            d = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("获取企微 access_token 异常：%s", e)
        return None
    if d.get("errcode") == 0 and d.get("access_token"):
        exp = now + int(d.get("expires_in", 7200))
        with _LOCK:
            _TOKEN_CACHE[key] = {"value": d["access_token"], "exp": exp}
        return d["access_token"]
    logger.warning("获取企微 access_token 失败：corpid=%s base=%s detail=%s", corpid, base, d)
    return None


def verify_token(corpid: str, corpsecret: str, base_url: str | None = None) -> dict:
    """配置页「验证连通性」用：真实请求一次 gettoken，返回结构化结果。

    不缓存失败结果；成功则顺手写入缓存，避免界面保存后立即重复换取。
    """
    if not corpid or not corpsecret:
        return {"ok": False, "errcode": -1, "errmsg": "corp_id 或 secret 为空"}
    try:
        with httpx.Client(timeout=10, proxy=settings.WECOM_PROXY or None) as client:
            r = client.get(
                f"{_base(base_url)}/cgi-bin/gettoken",
                params={"corpid": corpid, "corpsecret": corpsecret},
            )
            d = r.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "errcode": -2, "errmsg": f"请求失败：{e}"}
    if d.get("errcode") == 0 and d.get("access_token"):
        tok = d["access_token"]
        masked = (tok[:6] + "****" + tok[-4:]) if len(tok) > 12 else "****"
        with _LOCK:
            _TOKEN_CACHE[(corpid, corpsecret, _base(base_url))] = {
                "value": tok,
                "exp": time.time() + int(d.get("expires_in", 7200)),
            }
        return {
            "ok": True,
            "errcode": 0,
            "errmsg": "ok",
            "token_masked": masked,
            "expires_in": d.get("expires_in"),
        }
    return {"ok": False, "errcode": d.get("errcode"), "errmsg": d.get("errmsg")}


def get_access_token_from_settings() -> str | None:
    """用当前内存 settings 的 corpid + 应用 secret 取 token（告警通道复用）。"""
    return get_access_token(settings.WECOM_CORP_ID, settings.WECOM_AGENT_SECRET)
