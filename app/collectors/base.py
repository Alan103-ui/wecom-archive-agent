"""
app/collectors/base.py — 采集器抽象

把"消息从哪来"与"消息怎么处理"彻底解耦。
下游（落库→OCR→抽取）只认 NormalizedMessage，不关心数据来自：
    · 会话内容存档 SDK（archive）
    · 内置样例数据（mock）
    · 未来的智能机器人回调 / 第三方服务商推送

新增数据源 = 新写一个实现了 BaseCollector 的类，其余代码零改动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MediaRef:
    """消息里的一个媒体引用（尚未下载）"""

    media_type: str                 # image / file / video / voice / emotion
    sdkfileid: str
    file_name: str | None = None
    file_ext: str | None = None
    file_size: int = 0
    md5sum: str | None = None
    # mock 模式下直接给本地路径，跳过下载
    local_path: str | None = None


@dataclass
class NormalizedMessage:
    """归一化后的一条消息——采集层与业务层之间唯一的契约"""

    seq: int
    msgid: str
    msg_type: str
    action: str = "send"
    from_id: str = ""
    to_list: list[str] = field(default_factory=list)
    room_id: str = ""
    msg_time_ms: int = 0
    content_text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    medias: list[MediaRef] = field(default_factory=list)

    @property
    def msg_time(self) -> datetime | None:
        if not self.msg_time_ms:
            return None
        try:
            return datetime.fromtimestamp(self.msg_time_ms / 1000)
        except (ValueError, OSError, OverflowError):
            return None

    @property
    def is_group(self) -> bool:
        return bool(self.room_id)


class BaseCollector(ABC):
    """采集器接口"""

    name: str = "base"

    @abstractmethod
    def fetch(self, seq: int, limit: int) -> list[NormalizedMessage]:
        """
        从 seq 之后拉取至多 limit 条消息（返回的消息 seq 均 > 入参 seq）。
        返回空列表表示暂无新消息。
        """

    @abstractmethod
    def download_media(self, media: MediaRef, dest_path: str) -> int:
        """把媒体下载到 dest_path，返回写入字节数"""

    def health_check(self) -> tuple[bool, str]:
        """连通性自检，供 /health 与启动诊断使用"""
        return True, "ok"

    def close(self) -> None:
        """释放资源（默认无操作）"""
