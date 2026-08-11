"""
采集器工厂。

全局单例：SDK 句柄创建代价高（加载动态库 + 网络初始化），
且官方限制 GetChatData 调用频率 4000 次/分钟，共用一个实例更稳。
"""
from __future__ import annotations

import logging
import threading

from app.collectors.base import BaseCollector, MediaRef, NormalizedMessage
from app.config import settings

logger = logging.getLogger(__name__)

_instance: BaseCollector | None = None
_lock = threading.Lock()


def build_collector(mode: str | None = None) -> BaseCollector:
    """按模式构造采集器（不走单例，测试用）"""
    mode = (mode or settings.COLLECTOR_MODE).lower()

    if mode == "archive":
        from app.collectors.archive import ArchiveCollector

        return ArchiveCollector()

    if mode == "mock":
        from app.collectors.mock import MockCollector

        return MockCollector()

    raise ValueError(f"未知的 COLLECTOR_MODE={mode}，可选：mock / archive")


def get_collector() -> BaseCollector:
    """获取全局采集器单例"""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = build_collector()
                logger.info("采集器已就绪：mode=%s", _instance.name)
    return _instance


def reset_collector() -> None:
    """释放单例（配置变更或测试清理时调用）"""
    global _instance
    with _lock:
        if _instance is not None:
            try:
                _instance.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("采集器释放异常：%s", e)
            _instance = None


__all__ = [
    "BaseCollector",
    "MediaRef",
    "NormalizedMessage",
    "build_collector",
    "get_collector",
    "reset_collector",
]
