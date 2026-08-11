"""
app/services/wecom_api.py — 企业微信会话内容存档「辅助 HTTP 接口」封装

与「拉消息」(GetChatData SDK) 不同，下列接口走标准 HTTP + access_token：
  - groupchat/get          获取会话内容存档内部群信息（doc 92951）：群名/群主/成员
  - get_permit_user_list   获取会话内容存档开启成员列表（doc 16547）：ids 全量
  - check_single_agree     查询单聊会话中员工对会话存档的同意情况（status 1/2/3）
  - check_quit_list        获取已离职且需转接会话存档的成员列表（ids）

access_token 由 corpid + 「会话内容存档应用 secret」(archive_secret) 换取，复用 wecom_token 模块统一缓存。

注意分工：
  - 拉消息主链路 = SDK GetChatData（archive.py），不经 access_token。
  - 本模块只封装「拿到 roomid 后补充群信息、查哪些成员开了存档」这类只读辅助接口。
  - mock 模式下直接返回清晰的不可用提示，绝不伪造企业微信数据。
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.services.wecom_token import get_access_token

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://qyapi.weixin.qq.com"


class WeComAPIError(Exception):
    """企业微信接口返回非 0 errcode，或本地前置校验失败。"""

    def __init__(self, errcode: int, errmsg: str):
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"errcode={errcode} {errmsg}")


def _require_archive() -> None:
    if settings.COLLECTOR_MODE != "archive":
        raise WeComAPIError(
            -3, "当前为 mock 演示模式，切换为 archive 并配置有效凭证后可拉取真实群信息/成员"
        )


# 官方 10013：企业微信可能提前使 access_token 失效，需实现失效重取逻辑。
# 这些 errcode 代表 token 非法/失效/超时，捕获后强制刷新一次并重试。
_TOKEN_EXPIRED_ERRCODES = {40001, 40014, 41001, 42001, 42007}


def _post(path: str, payload: dict, corpid: str | None, corpsecret: str | None, base_url: str | None) -> dict:
    """携带 access_token POST 企业微信 msgaudit 系列接口，返回已校验 errcode 的 JSON。

    secret 归属（官方 16547 / 92951 权限说明）：
        msgaudit/* 接口（groupchat/get、get_permit_user_list 等）必须用
        「会话内容存档应用 secret」(archive_secret) 换取的 access_token 调用。
        默认优先 archive_secret，未配置时 fallback 到 agent_secret 以兼容简化部署。
    token 失效：遇到 40001/40014/41001/42001/42007 时强制刷新一次并重试（最多 2 次）。
    """
    corpid = (corpid or settings.WECOM_CORP_ID or "").strip()
    corpsecret = (corpsecret or settings.WECOM_ARCHIVE_SECRET or settings.WECOM_AGENT_SECRET or "").strip()
    base = (base_url or settings.WECOM_API_BASE_URL or _DEFAULT_BASE).rstrip("/")

    token = get_access_token(corpid, corpsecret, base_url)
    if not token:
        raise WeComAPIError(-2, "无法获取 access_token：corp_id 或 会话存档 Secret 为空，或换取失败")

    for attempt in range(2):
        try:
            with httpx.Client(timeout=10, proxy=settings.WECOM_PROXY or None) as client:
                r = client.post(f"{base}{path}", params={"access_token": token}, json=payload)
                d = r.json()
        except Exception as e:  # noqa: BLE001
            raise WeComAPIError(-99, f"请求失败：{e}")

        ec = d.get("errcode")
        if ec in _TOKEN_EXPIRED_ERRCODES:
            # token 失效/非法：强制刷新一次后重试（官方 10013 要求失效重取）
            if attempt == 1:
                raise WeComAPIError(int(ec), d.get("errmsg") or "access_token 失效且刷新后仍失败")
            token = get_access_token(corpid, corpsecret, base_url, force=True)
            continue
        if ec not in (0, None):
            raise WeComAPIError(int(ec), d.get("errmsg") or "unknown")
        return d
    raise WeComAPIError(-99, "重试逻辑异常")  # 理论上不会到达


def get_group_chat(roomid: str, corpid: str | None = None, corpsecret: str | None = None, base_url: str | None = None) -> dict:
    """获取会话内容存档内部群信息（官方 msgaudit/groupchat/get，doc 92951）并规整。

    官方返回字段：roomid / roomname / creator / room_create_time / notice /
    members[{memberid, jointime}]。群主字段官方命名为 creator（旧版文档亦称 owner），
    此处两者兼容取值。
    返回 {"roomid","roomname","owner","member_count","members":[userid...]}
    """
    _require_archive()
    d = _post("/cgi-bin/msgaudit/groupchat/get", {"roomid": roomid}, corpid, corpsecret, base_url)
    grp = d.get("group") or {}
    members = [m.get("memberid") for m in grp.get("members", []) if m.get("memberid")]
    owner = grp.get("owner") or grp.get("creator")  # 官方群主字段为 creator
    return {
        "roomid": grp.get("roomid", roomid),
        "roomname": grp.get("roomname"),
        "owner": owner,
        "member_count": len(members),
        "members": members,
    }


def get_permit_user_list(
    corpid: str | None = None,
    corpsecret: str | None = None,
    base_url: str | None = None,
    edition_type: int | None = None,
) -> dict:
    """获取已开启会话内容存档的成员列表（官方 msgaudit/get_permit_user_list，doc 16547）。

    官方返回 {"errcode":0,"errmsg":"ok","ids":["userid_1",...]}：一次性返回全量 userid，
    无分页游标。可选请求参数 type：1=办公版 2=服务版 3=企业版，None=返回全量。
    token 必须用「会话内容存档应用 secret」换取（官方权限说明）。
    对外统一封装为 {"userlist":[userid...], "count":int}，保持路由层兼容。
    """
    _require_archive()
    payload: dict = {}
    if edition_type is not None:
        payload["type"] = edition_type
    d = _post("/cgi-bin/msgaudit/get_permit_user_list", payload, corpid, corpsecret, base_url)
    ids = d.get("ids") or []
    return {"userlist": ids, "count": len(ids)}


def check_single_agree(
    roomids: list[str],
    userid: str,
    corpid: str | None = None,
    corpsecret: str | None = None,
    base_url: str | None = None,
) -> dict:
    """查询单聊会话中，员工对会话内容存档的同意情况（官方 msgaudit/check_single_agree）。

    请求体：roomid（会话 ID 列表，单次最多 100 个）、userid（待查询员工 userid）。
    返回字段 agree_status：[{roomid, status}]，status 取值：
        1 = 同意存档；2 = 不同意存档；3 = 未设置（未询问）。
    这里附 status_text 便于前端直接展示。
    """
    _require_archive()
    if not roomids:
        raise WeComAPIError(-101, "roomids 不能为空")
    if not userid:
        raise WeComAPIError(-102, "userid 不能为空")
    payload = {"roomid": list(roomids)[:100], "userid": userid}
    d = _post("/cgi-bin/msgaudit/check_single_agree", payload, corpid, corpsecret, base_url)
    raw = d.get("agree_status") or []
    status_map = {1: "同意存档", 2: "不同意存档", 3: "未设置(未询问)"}
    items = []
    for it in raw:
        st = it.get("status")
        items.append({
            "roomid": it.get("roomid"),
            "status": st,
            "status_text": status_map.get(st, "未知"),
        })
    return {"agree_status": items, "count": len(items)}


def get_quit_list(
    corpid: str | None = None,
    corpsecret: str | None = None,
    base_url: str | None = None,
) -> dict:
    """获取已离职且需要转接会话内容存档的成员列表（官方 msgaudit/check_quit_list）。

    请求体为空。返回字段 ids：已离职成员 userid 数组（需由其他在职成员转接其会话）。
    """
    _require_archive()
    d = _post("/cgi-bin/msgaudit/check_quit_list", {}, corpid, corpsecret, base_url)
    ids = d.get("ids") or []
    return {"ids": ids, "count": len(ids)}
