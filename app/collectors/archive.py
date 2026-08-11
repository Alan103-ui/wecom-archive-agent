"""
app/collectors/archive.py — 会话内容存档采集器（生产路径）

完整链路：
    GetChatData(密文)
      → RSA 私钥解 encrypt_random_key 得对称密钥
      → SDK DecryptData 解 encrypt_chat_msg 得明文 JSON
      → 归一化成 NormalizedMessage
      → 媒体走 GetMediaData 分片下载

容错原则：**单条消息解密失败不能拖垮整批**。
会话存档只保留最近 5 天，一条坏消息导致整批中断 = 永久丢数据。
因此逐条 try/except，失败的记日志并跳过，游标照常推进。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.collectors.base import BaseCollector, MediaRef, NormalizedMessage
from app.collectors.crypto import RandomKeyDecryptor
from app.collectors.sdk import WeWorkFinanceSdk, WeWorkSdkError
from app.config import settings

logger = logging.getLogger(__name__)

# 各消息类型中媒体字段的位置：msgtype → (json_key, 归一化后的 media_type)
_MEDIA_FIELDS: dict[str, tuple[str, str]] = {
    "image": ("image", "image"),
    "file": ("file", "file"),
    "video": ("video", "video"),
    "voice": ("voice", "voice"),
    "emotion": ("emotion", "emotion"),
}


def _rich_summary(msg_type: str, plain: dict[str, Any]) -> str:
    """把富类型消息抽成一行可读摘要，供检索 / 风险检测命中。

    官方支持但此前走 else 占位（content_text 永远为 ``[type]``）的类型集中在此处理。
    字段取法遵循企业微信解密 JSON 的常见结构，全部用 ``.get`` 兜底，绝不抛错。
    """
    if msg_type == "todo":
        t = plain.get("todo", {}) or {}
        return f"[待办] {t.get('title', '')} {t.get('content', '')}".strip()
    if msg_type == "vote":
        t = plain.get("vote", {}) or {}
        opts = t.get("options") or []
        opt_txt = ""
        if isinstance(opts, list):
            opt_txt = " / ".join(
                str(o.get("text", "")) for o in opts if isinstance(o, dict)
            )
        return f"[投票] {t.get('title', '')} {t.get('desc', '')} {opt_txt}".strip()
    if msg_type in ("collect", "template_card"):
        # 填表消息早期为 collect，新模板卡片为 template_card
        t = (plain.get("collect") or plain.get("template_card") or {}) or {}
        return f"[填表] {t.get('title', '')}".strip()
    if msg_type == "redpacket":
        t = plain.get("redpacket", {}) or {}
        return f"[红包] {t.get('title', '')}".strip()
    if msg_type == "meeting":
        t = plain.get("meeting", {}) or {}
        return f"[会议] {t.get('title', '')}".strip()
    if msg_type == "doc":
        t = plain.get("doc", {}) or {}
        return f"[文档] {t.get('title', '')} {t.get('link_url', '')}".strip()
    if msg_type == "news":
        t = plain.get("news", {}) or {}
        items = t.get("item", []) or []
        if items and isinstance(items[0], dict):
            t = items[0]
        return f"[图文] {t.get('title', '')} {t.get('description', '')}".strip()
    if msg_type == "calendar":
        t = plain.get("calendar", {}) or {}
        return f"[日程] {t.get('title', '')}".strip()
    if msg_type == "channel":
        t = plain.get("channel", {}) or {}
        return f"[视频号] {t.get('title', '')}".strip()
    if msg_type == "markdown":
        t = plain.get("markdown", {}) or {}
        return f"[MD] {t.get('content', '')}".strip()
    return f"[{msg_type}]"


class ArchiveCollector(BaseCollector):
    name = "archive"

    def __init__(self):
        self.sdk = WeWorkFinanceSdk(
            lib_path=settings.WECOM_SDK_PATH,
            corp_id=settings.WECOM_CORP_ID,
            secret=settings.WECOM_ARCHIVE_SECRET,
            proxy=settings.WECOM_PROXY,
            proxy_passwd=settings.WECOM_PROXY_PASSWD,
            timeout=settings.WECOM_SDK_TIMEOUT,
        )
        self.decryptor = RandomKeyDecryptor(
            default_key_path=settings.WECOM_PRIVATE_KEY_PATH,
            key_map=settings.WECOM_PRIVATE_KEY_MAP,
        )
        self._decrypt_fail = 0

    # ------------------------------------------------------------------
    def health_check(self) -> tuple[bool, str]:
        try:
            self.sdk.init()
        except WeWorkSdkError as e:
            return False, str(e)

        missing = []
        if not Path(settings.WECOM_PRIVATE_KEY_PATH).exists() and not settings.WECOM_PRIVATE_KEY_MAP:
            missing.append(f"RSA 私钥 {settings.WECOM_PRIVATE_KEY_PATH}")
        if missing:
            return False, "缺少：" + "、".join(missing)
        return True, "会话存档 SDK 就绪"

    def close(self) -> None:
        self.sdk.close()

    # ------------------------------------------------------------------
    def fetch(self, seq: int, limit: int) -> list[NormalizedMessage]:
        raw_items = self.sdk.get_chat_data(seq=seq, limit=limit)
        if not raw_items:
            return []

        messages: list[NormalizedMessage] = []
        for item in raw_items:
            item_seq = int(item.get("seq", 0))
            try:
                aes_key = self.decryptor.decrypt(
                    item["encrypt_random_key"], item.get("publickey_ver")
                )
                plain = self.sdk.decrypt_data(aes_key, item["encrypt_chat_msg"])
            except Exception as e:  # noqa: BLE001
                # 关键：不中断整批。记下 seq，后续可用 /sync/replay 单独重放
                self._decrypt_fail += 1
                logger.error(
                    "消息解密失败 seq=%s msgid=%s：%s",
                    item_seq, item.get("msgid", "")[:32], e,
                )
                continue

            try:
                messages.append(self._normalize(item_seq, plain))
            except Exception as e:  # noqa: BLE001
                logger.error("消息归一化失败 seq=%s：%s", item_seq, e)

        return messages

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(seq: int, plain: dict[str, Any]) -> NormalizedMessage:
        msg_type = plain.get("msgtype", "unknown")
        msg = NormalizedMessage(
            seq=seq,
            msgid=plain.get("msgid", f"seq_{seq}"),
            msg_type=msg_type,
            action=plain.get("action", "send"),
            from_id=plain.get("from", "") or "",
            to_list=plain.get("tolist") or [],
            room_id=plain.get("roomid", "") or "",
            msg_time_ms=int(plain.get("msgtime", 0) or 0),
            raw=plain,
        )

        body = plain.get(msg_type) if isinstance(plain.get(msg_type), dict) else {}

        # ---- 文本类：直接取正文 ----
        if msg_type == "text":
            msg.content_text = body.get("content", "")

        # ---- 媒体类：登记待下载资源 ----
        elif msg_type in _MEDIA_FIELDS:
            key, media_type = _MEDIA_FIELDS[msg_type]
            b = plain.get(key) or {}
            sdkfileid = b.get("sdkfileid")
            if sdkfileid:
                file_name = b.get("filename")
                file_ext = b.get("fileext")
                if not file_ext and file_name and "." in file_name:
                    file_ext = file_name.rsplit(".", 1)[-1]
                # 图片消息不带文件名，用 md5 兜底命名
                if not file_name:
                    guess_ext = file_ext or ("jpg" if media_type in ("image", "emotion") else "bin")
                    file_name = f"{b.get('md5sum', 'media')}.{guess_ext}"
                    file_ext = guess_ext

                size = int(
                    b.get("filesize") or b.get("voice_size") or b.get("imagesize") or 0
                )
                msg.medias.append(
                    MediaRef(
                        media_type=media_type,
                        sdkfileid=sdkfileid,
                        file_name=file_name,
                        file_ext=f".{file_ext.lstrip('.').lower()}" if file_ext else None,
                        file_size=size,
                        md5sum=b.get("md5sum"),
                    )
                )
                msg.content_text = f"[{media_type}] {file_name}"

        # ---- 图文混排：文本与图片可能都有 ----
        elif msg_type == "mixed":
            texts: list[str] = []
            for idx, sub in enumerate(plain.get("mixed", {}).get("item", []) or []):
                sub_type = sub.get("type")
                content = sub.get("content")
                # item.content 是 JSON 字符串
                if isinstance(content, str):
                    import json as _json

                    try:
                        content = _json.loads(content)
                    except _json.JSONDecodeError:
                        content = {"content": content}
                content = content or {}

                if sub_type == "text":
                    texts.append(str(content.get("content", "")))
                elif sub_type in ("image", "file", "video"):
                    fid = content.get("sdkfileid")
                    if fid:
                        fname = content.get("filename") or f"mixed_{idx}.jpg"
                        ext = content.get("fileext") or fname.rsplit(".", 1)[-1]
                        msg.medias.append(
                            MediaRef(
                                media_type=sub_type,
                                sdkfileid=fid,
                                file_name=fname,
                                file_ext=f".{str(ext).lstrip('.').lower()}",
                                file_size=int(content.get("filesize") or 0),
                                md5sum=content.get("md5sum"),
                            )
                        )
            msg.content_text = "\n".join(t for t in texts if t)

        # ---- 撤回 / 链接 / 其他富类型：留摘要，原文进 raw 兜底 ----
        elif msg_type == "revoke":
            msg.content_text = f"[撤回] 原消息 {plain.get('revoke', {}).get('pre_msgid', '')}"
        elif msg_type == "link":
            lk = plain.get("link", {})
            msg.content_text = f"[链接] {lk.get('title', '')} {lk.get('link_url', '')}".strip()
        elif msg_type == "agree":
            ag = plain.get("agree", {}) or {}
            msg.content_text = f"[同意存档] {ag.get('userid', '')}".strip()
        elif msg_type == "card":
            cd = plain.get("card", {}) or {}
            msg.content_text = f"[名片] {cd.get('name', '')} {cd.get('corpname', '')}".strip()
        elif msg_type == "location":
            loc = plain.get("location", {}) or {}
            msg.content_text = f"[位置] {loc.get('title', '')} {loc.get('address', '')}".strip()
        elif msg_type == "weapp":
            wa = plain.get("weapp", {}) or {}
            msg.content_text = f"[小程序] {wa.get('displayname', '')}".strip()
        elif msg_type == "chatrecord":
            cr = plain.get("chatrecord", {}) or {}
            items = cr.get("item", []) or []
            snippets: list[str] = []
            for it in items[:8]:
                if not isinstance(it, dict):
                    continue
                t = it.get("msgtype")
                sub = it.get(t, {}) if t and isinstance(it.get(t), dict) else {}
                if t == "text":
                    snippets.append(str(sub.get("content", "")))
                elif t in ("image", "file", "video", "voice", "emotion"):
                    snippets.append(f"[{t}]")
                elif t == "revoke":
                    snippets.append("[撤回]")
                elif t == "link":
                    snippets.append(f"[链接] {sub.get('title', '')}")
                elif t == "location":
                    snippets.append(f"[位置] {sub.get('title', '')}")
                elif t == "weapp":
                    snippets.append(f"[小程序] {sub.get('displayname', '')}")
            msg.content_text = f"[会话记录] {cr.get('title', '')}".strip()
            if snippets:
                msg.content_text += "：" + "；".join(s for s in snippets if s)
        elif msg_type in (
            "todo",
            "vote",
            "collect",
            "template_card",
            "redpacket",
            "meeting",
            "doc",
            "news",
            "calendar",
            "channel",
            "markdown",
        ):
            msg.content_text = _rich_summary(msg_type, plain)
        else:
            # 兜底：尽量从子对象抽可读文本，避免 content_text 永远为空壳
            sub = plain.get(msg_type)
            if isinstance(sub, dict):
                bits = [str(v) for k, v in sub.items() if isinstance(v, str) and v]
                msg.content_text = (f"[{msg_type}] " + " ".join(bits[:3])) if bits else f"[{msg_type}]"
            else:
                msg.content_text = f"[{msg_type}]"

        return msg

    # ------------------------------------------------------------------
    def download_media(self, media: MediaRef, dest_path: str) -> int:
        max_bytes = settings.MEDIA_MAX_SIZE_MB * 1024 * 1024
        if media.file_size and media.file_size > max_bytes:
            raise ValueError(
                f"文件 {media.file_name} 大小 {media.file_size / 1048576:.1f}MB "
                f"超过上限 {settings.MEDIA_MAX_SIZE_MB}MB，已跳过"
            )

        blob = self.sdk.get_media_data(media.sdkfileid, max_bytes=max_bytes)
        if not blob:
            raise ValueError("下载到 0 字节，可能 sdkfileid 已过期（存档仅保留 5 天）")

        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)

        # md5 校验，防止分片拼接错乱
        if media.md5sum:
            import hashlib

            actual = hashlib.md5(blob).hexdigest()
            if actual.lower() != media.md5sum.lower():
                logger.warning(
                    "媒体 md5 不匹配 file=%s 期望=%s 实际=%s", media.file_name, media.md5sum, actual
                )

        return len(blob)
