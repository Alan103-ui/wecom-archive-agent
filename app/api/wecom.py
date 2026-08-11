"""
app/api/wecom.py — 企业微信辅助接口（群信息 / 存档成员 / 同意情况 / 离职转接）

  GET  /api/wecom/groupchat/{room_id}  从企业微信拉取群信息并写回群档案（ChatRoom）
  GET  /api/wecom/permit-users         拉取已开启会话内容存档的成员列表
  POST /api/wecom/single-agree         查询单聊会话存档同意情况（check_single_agree）
  GET  /api/wecom/quit-list            拉取已离职需转接会话的成员列表（check_quit_list）

这些接口仅在 archive 模式 + 有效凭证下可用；mock 模式返回清晰的不可用提示。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.entities import ChatRoom
from app.services import wecom_api

router = APIRouter()


class SingleAgreeIn(BaseModel):
    roomids: list[str] = []
    userid: str = ""


@router.get("/groupchat/{room_id}", summary="从企业微信拉取群信息并写回群档案")
def groupchat(room_id: str, db: Session = Depends(get_db)):
    try:
        info = wecom_api.get_group_chat(room_id)
    except wecom_api.WeComAPIError as e:
        raise HTTPException(400, f"errcode={e.errcode} {e.errmsg}")

    room = db.get(ChatRoom, room_id)
    if room is None:
        room = ChatRoom(
            room_id=room_id,
            name=info.get("roomname") or None,
            owner=info.get("owner"),
            member_count=info.get("member_count", 0),
        )
        db.add(room)
    else:
        if info.get("roomname"):
            room.name = info.get("roomname")
        if info.get("owner"):
            room.owner = info.get("owner")
        room.member_count = info.get("member_count", 0)
    room.members = ",".join(info.get("members") or [])
    db.commit()
    db.refresh(room)

    return {
        "ok": True,
        "room_id": room_id,
        "name": room.name,
        "owner": room.owner,
        "member_count": room.member_count,
        "members": info.get("members") or [],
    }


@router.get("/permit-users", summary="拉取已开启会话内容存档的成员列表")
def permit_users():
    try:
        d = wecom_api.get_permit_user_list()
    except wecom_api.WeComAPIError as e:
        raise HTTPException(400, f"errcode={e.errcode} {e.errmsg}")
    return {"ok": True, "count": d["count"], "userlist": d["userlist"]}


@router.post("/single-agree", summary="查询单聊会话存档同意情况")
def single_agree(body: SingleAgreeIn):
    try:
        d = wecom_api.check_single_agree(body.roomids, body.userid)
    except wecom_api.WeComAPIError as e:
        raise HTTPException(400, f"errcode={e.errcode} {e.errmsg}")
    return {"ok": True, "count": d["count"], "agree_status": d["agree_status"]}


@router.get("/quit-list", summary="拉取已离职需转接会话的成员列表")
def quit_list():
    try:
        d = wecom_api.get_quit_list()
    except wecom_api.WeComAPIError as e:
        raise HTTPException(400, f"errcode={e.errcode} {e.errmsg}")
    return {"ok": True, "count": d["count"], "ids": d["ids"]}
