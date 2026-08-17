"""
app/services/ocr/pool.py — 受管持久 OCR 进程池（崩溃隔离 + 热复用）

为什么需要它：
    RapidOCR 的 C 层（ONNXRuntime / PP-OCRv4）在畸形图上会**静默段错误 / OOM**，
    Python 的 try 无法捕获，会直接杀死宿主进程。而生产环境里 process_attachments()
    由 APScheduler 与 API 端点**都在同一个 uvicorn 进程**中调用，OCR 走进程内单例——
    一张坏图就能把整个服务（API + 调度器 + 所有在途任务）一起带走。

本模块把 OCR 放进独立 worker 进程：
    · 热复用：每个 worker 内 RapidOCR 只加载一次，跨多张图复用（快）。
    · 崩溃隔离：段错误只杀掉 worker 进程，uvicorn 主进程安然无恙（安全）。
    · 自动复活：worker 意外退出（BrokenProcessPool）时，池自动重建。
    · 优雅降级：池创建/执行失败则回退进程内执行，保留功能（此时无隔离，仅兜底）。

注意：worker 进程里跑的是 engine.recognize()（含 RapidOCR + 可选的视觉模型二次识别），
      它以模块级函数被 ProcessPoolExecutor 跨进程 pickle 调用，无需把大对象传进 worker。
"""
from __future__ import annotations

import atexit
import logging
import threading
from concurrent.futures import ProcessPoolExecutor
# 跨 Python 版本稳健导入：BrokenProcessPool 在部分发行版顶层未导出
try:  # noqa: E402
    from concurrent.futures import BrokenProcessPool
except ImportError:  # pragma: no cover
    from concurrent.futures.process import BrokenProcessPool
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_pool: Optional[ProcessPoolExecutor] = None
# 池是否已确认不可用（创建失败过），用于避免反复重试拖垮请求
_pool_broken = False


def _get_pool() -> Optional[ProcessPoolExecutor]:
    """懒创建受管进程池；返回 None 表示当前不可用（调用方应降级）。"""
    global _pool, _pool_broken
    with _lock:
        if _pool is not None:
            return _pool
        if _pool_broken:
            return None
        try:
            workers = max(1, int(settings.OCR_POOL_WORKERS))
            _pool = ProcessPoolExecutor(max_workers=workers)
            logger.info("OCR 受管进程池已创建（workers=%d）", workers)
            return _pool
        except Exception as e:  # noqa: BLE001
            _pool_broken = True
            logger.error("OCR 进程池创建失败，后续将降级为进程内执行：%s", e)
            return None


def _shutdown_pool() -> None:
    """关闭并置空进程池（进程退出 / 池损坏重建时调用）。"""
    global _pool, _pool_broken
    with _lock:
        if _pool is not None:
            try:
                _pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
            _pool = None
        _pool_broken = False


# 服务退出时清理 worker，避免孤儿进程
atexit.register(_shutdown_pool)


def recognize_isolated(file_path, force_vision: bool = False):
    """
    隔离执行 OCR。RapidOCR 的 C 层崩溃只会影响 worker，不会杀死 uvicorn。

    :param file_path: 图片/PDF 路径
    :param force_vision: 是否强制走视觉模型 OCR（模板强制复杂版式时）
    :return: OcrOutcome
    """
    # 延迟导入，避免与 engine 形成模块级循环依赖
    from app.services.ocr.engine import OcrOutcome, recognize as _recognize_sync

    timeout = int(settings.OCR_POOL_TIMEOUT)
    pool = _get_pool()

    if pool is None:
        # 池不可用：降级进程内（保留功能，但失去隔离）
        logger.warning("OCR 进程池不可用，降级为进程内执行（无隔离）path=%s", file_path)
        return _recognize_sync(str(file_path), force_vision=force_vision)

    try:
        fut = pool.submit(_recognize_sync, str(file_path), force_vision)
        return fut.result(timeout=timeout)
    except BrokenProcessPool:
        # worker 段错误导致池损坏：重建后重试一次
        logger.warning("OCR 进程池损坏（疑似 worker 段错误），正在重建…")
        _shutdown_pool()
        pool = _get_pool()
        if pool is None:
            return _recognize_sync(str(file_path), force_vision=force_vision)
        try:
            return pool.submit(_recognize_sync, str(file_path), force_vision).result(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            logger.error("OCR 池重建后仍失败，降级进程内：%s", e)
            return _recognize_sync(str(file_path), force_vision=force_vision)
    except TimeoutError:
        logger.warning("OCR 进程池任务超时（>%ds），降级进程内执行 path=%s", timeout, file_path)
        return _recognize_sync(str(file_path), force_vision=force_vision)
    except Exception as e:  # noqa: BLE001
        logger.error("OCR 进程池执行异常，降级进程内：%s", e)
        return _recognize_sync(str(file_path), force_vision=force_vision)


def pool_status() -> dict:
    """供 engine_status() 使用：报告池是否存活，**不在主进程加载 OCR 模型**。"""
    with _lock:
        alive = _pool is not None
    return {
        "pool_mode": True,
        "pool_alive": alive,
        "workers": int(settings.OCR_POOL_WORKERS),
        "broken": _pool_broken,
        "note": "OCR 在独立 worker 进程执行（崩溃隔离），不在此进程加载模型",
    }
