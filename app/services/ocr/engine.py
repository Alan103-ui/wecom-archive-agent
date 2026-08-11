"""
app/services/ocr/engine.py — OCR 引擎封装

选型：RapidOCR（PP-OCRv4 模型 + ONNXRuntime）
  · 纯 CPU 可用，不吃显卡，与本地 Ollama 抢不到资源也无妨
  · 免去 PaddlePaddle 的版本地狱（invoice-ocr 项目已验证过这个坑）
  · 中文识别质量对齐 PaddleOCR

支持输入：
  · 图片：jpg/png/bmp/webp/tiff/gif
  · PDF ：优先抽取文本层（矢量 PDF 秒出且 100% 准确），
          文本层为空（扫描件）才逐页渲染成图再 OCR

模型首次加载约 2~5 秒，故做进程内单例懒加载。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class OcrBlock:
    text: str
    score: float
    box: list[list[float]] = field(default_factory=list)
    page: int = 1


@dataclass
class OcrOutcome:
    success: bool
    text: str = ""
    blocks: list[OcrBlock] = field(default_factory=list)
    page_count: int = 1
    avg_confidence: float | None = None
    duration_ms: int = 0
    engine: str = "rapidocr"
    error: str | None = None

    def blocks_as_dict(self) -> list[dict]:
        return [{"text": b.text, "score": round(b.score, 4), "box": b.box, "page": b.page} for b in self.blocks]


class _EngineHolder:
    """RapidOCR 单例。模型加载耗时，且实例非线程安全，故加锁串行使用"""

    _engine = None
    _lock = threading.Lock()
    _load_error: str | None = None

    @classmethod
    def get(cls):
        if cls._engine is None:
            with cls._lock:
                if cls._engine is None and cls._load_error is None:
                    try:
                        from rapidocr_onnxruntime import RapidOCR

                        t0 = time.time()
                        cls._engine = RapidOCR()
                        logger.info("RapidOCR 模型加载完成，耗时 %.2fs", time.time() - t0)
                    except ImportError as e:
                        cls._load_error = (
                            f"未安装 OCR 依赖：{e}。请执行 pip install rapidocr-onnxruntime"
                        )
                        logger.error(cls._load_error)
                    except Exception as e:  # noqa: BLE001
                        cls._load_error = f"OCR 引擎初始化失败：{e}"
                        logger.error(cls._load_error)
        if cls._load_error:
            raise RuntimeError(cls._load_error)
        return cls._engine

    @classmethod
    def lock(cls):
        return cls._lock


def _run_rapidocr(image_input, page: int = 1) -> tuple[list[OcrBlock], float | None]:
    """对单张图执行 OCR。image_input 可以是路径字符串或 numpy 数组"""
    engine = _EngineHolder.get()
    with _EngineHolder.lock():
        result, _elapse = engine(image_input)

    if not result:
        return [], None

    blocks: list[OcrBlock] = []
    scores: list[float] = []
    for item in result:
        # RapidOCR 返回 [box, text, score]
        try:
            box, text, score = item[0], item[1], float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if not text or not str(text).strip():
            continue
        norm_box = [[float(x), float(y)] for x, y in box] if box else []
        blocks.append(OcrBlock(text=str(text).strip(), score=score, box=norm_box, page=page))
        scores.append(score)

    avg = sum(scores) / len(scores) if scores else None
    return blocks, avg


# ---------------------------------------------------------------- PDF
def _ocr_pdf(path: Path) -> OcrOutcome:
    t0 = time.time()
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return OcrOutcome(success=False, error="未安装 pymupdf，无法处理 PDF")

    try:
        doc = fitz.open(str(path))
    except Exception as e:  # noqa: BLE001
        return OcrOutcome(success=False, error=f"PDF 打开失败：{e}")

    total_pages = doc.page_count
    pages = min(total_pages, settings.OCR_PDF_MAX_PAGES)
    all_blocks: list[OcrBlock] = []
    all_scores: list[float] = []
    text_layer_parts: list[str] = []

    try:
        # 第一遍：尝试直接取文本层
        for i in range(pages):
            txt = doc.load_page(i).get_text().strip()
            if txt:
                text_layer_parts.append(txt)

        joined = "\n".join(text_layer_parts).strip()
        # 文本层够用就不做 OCR：既快又准
        if len(joined) >= 20:
            doc.close()
            return OcrOutcome(
                success=True,
                text=joined,
                blocks=[
                    OcrBlock(text=t, score=1.0, page=i + 1)
                    for i, t in enumerate(text_layer_parts)
                ],
                page_count=pages,
                avg_confidence=1.0,
                duration_ms=int((time.time() - t0) * 1000),
                engine="pdf-textlayer",
            )

        # 第二遍：扫描件，逐页渲染后 OCR
        import numpy as np
        from PIL import Image

        zoom = settings.OCR_PDF_DPI / 72.0
        mat = fitz.Matrix(zoom, zoom)
        for i in range(pages):
            pix = doc.load_page(i).get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            blocks, avg = _run_rapidocr(np.array(img), page=i + 1)
            all_blocks.extend(blocks)
            if avg is not None:
                all_scores.append(avg)
        doc.close()
    except Exception as e:  # noqa: BLE001
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
        return OcrOutcome(success=False, error=f"PDF 识别失败：{e}")

    return OcrOutcome(
        success=True,
        text="\n".join(b.text for b in all_blocks),
        blocks=all_blocks,
        page_count=pages,
        avg_confidence=(sum(all_scores) / len(all_scores)) if all_scores else None,
        duration_ms=int((time.time() - t0) * 1000),
        engine="rapidocr-pdf",
    )


# ---------------------------------------------------------------- 入口
def recognize(file_path: str | Path) -> OcrOutcome:
    """
    识别一个文件。永不抛异常——失败信息塞进 OcrOutcome.error，
    让流水线可以统一处理并落库，不会因为一个坏文件中断整批。
    """
    path = Path(file_path)
    if not path.exists():
        return OcrOutcome(success=False, error=f"文件不存在：{path}")

    ext = path.suffix.lower()

    if ext in settings.OCR_PDF_EXTS:
        return _ocr_pdf(path)

    if ext not in settings.OCR_IMAGE_EXTS:
        return OcrOutcome(success=False, error=f"不支持 OCR 的文件类型：{ext}")

    t0 = time.time()
    try:
        blocks, avg = _run_rapidocr(str(path))
    except Exception as e:  # noqa: BLE001
        return OcrOutcome(success=False, error=f"OCR 执行失败：{e}")

    return OcrOutcome(
        success=True,
        text="\n".join(b.text for b in blocks),
        blocks=blocks,
        page_count=1,
        avg_confidence=avg,
        duration_ms=int((time.time() - t0) * 1000),
    )


def is_ocr_supported(ext: str | None) -> bool:
    if not ext:
        return False
    e = ext if ext.startswith(".") else f".{ext}"
    e = e.lower()
    return e in settings.OCR_IMAGE_EXTS or e in settings.OCR_PDF_EXTS


def engine_status() -> dict:
    """供 /health 展示"""
    try:
        _EngineHolder.get()
        return {"available": True, "engine": "rapidocr-onnxruntime"}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)}
