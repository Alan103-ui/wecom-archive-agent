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

import base64
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


# ---------------------------------------------------------------- 视觉模型 OCR 升级（混合识别）
OCR_VISION_PROMPT = (
    "请完整、忠实地转录图片中的全部文字，保持原有的段落顺序与表格结构"
    "（表格用制表符或空格对齐行列，不要丢列）。\n"
    "要求：\n"
    "1. 只输出图片里的文字内容，不要做任何解释、不要补充图片外信息；\n"
    "2. 字迹不清处按最可能的内容转录，不要臆造不存在的数字或金额；\n"
    "3. 若图片无文字，输出空字符串。"
)


def _ocr_vision_policy() -> tuple[bool, float]:
    """当前是否启用视觉升级、以及触发阈值。抽成函数便于测试 monkeypatch。KV 优先，config 兜底。"""
    from app.services.kv_store import get_setting
    enabled = get_setting("OCR_VISION_ENABLED", settings.OCR_VISION_ENABLED)
    threshold = get_setting("OCR_VISION_MIN_CONFIDENCE", settings.OCR_VISION_MIN_CONFIDENCE)
    return bool(enabled), float(threshold)


def _get_vision_model():
    """取视觉模型连接（复用「视觉抽取(多模态)」用途，即 glm-4v 等）。无则 None。"""
    try:
        from app.services.llm.client import get_model_for_role
        from app.models.model_config import ROLE_EXTRACT_VISION
    except Exception:  # noqa: BLE001
        return None
    try:
        return get_model_for_role(ROLE_EXTRACT_VISION, fallback=False)
    except Exception:  # noqa: BLE001
        return None


def _encode_image(path: Path) -> tuple[str, str]:
    """图片转 base64 及 media type（仅图片；PDF 走 _ocr_pdf，不会到这里）。"""
    ext = path.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".bmp": "image/bmp", ".webp": "image/webp", ".gif": "image/gif",
        ".tiff": "image/tiff",
    }.get(ext, "image/png")
    return base64.b64encode(path.read_bytes()).decode("ascii"), media_type


def _vision_recognize_text(image_b64: str, media_type: str, config) -> str:
    """调用视觉模型把图片转成纯文本（网络调用，可能抛 LlmError）。"""
    from app.services.llm.client import vision_to_text

    return vision_to_text(config, image_b64, OCR_VISION_PROMPT, image_media_type=media_type)


def _run_image_rapidocr(path: Path) -> OcrOutcome:
    """对单张图跑 RapidOCR，封装成 OcrOutcome。"""
    t0 = time.time()
    try:
        blocks, avg = _run_rapidocr(str(path))
    except Exception as e:  # noqa: BLE001
        return OcrOutcome(success=False, error=f"OCR 执行失败：{e}", engine="rapidocr")
    return OcrOutcome(
        success=True,
        text="\n".join(b.text for b in blocks),
        blocks=blocks,
        page_count=1,
        avg_confidence=avg,
        duration_ms=int((time.time() - t0) * 1000),
        engine="rapidocr",
    )


def _run_vision(path: Path, config) -> OcrOutcome:
    """用视觉模型识别图片，封装成 OcrOutcome。模型不返回坐标，blocks 为空。"""
    t0 = time.time()
    try:
        image_b64, media_type = _encode_image(path)
        text = _vision_recognize_text(image_b64, media_type, config)
    except Exception as e:  # noqa: BLE001
        return OcrOutcome(success=False, error=f"视觉识别失败：{e}", engine="vision-ocr")
    text = (text or "").strip()
    return OcrOutcome(
        success=bool(text),
        text=text,
        blocks=[],
        page_count=1,
        avg_confidence=None,
        duration_ms=int((time.time() - t0) * 1000),
        engine="vision-ocr",
        error=None if text else "视觉模型未返回文本",
    )


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
def recognize(file_path: str | Path, force_vision: bool = False) -> OcrOutcome:
    """
    识别一个文件。永不抛异常——失败信息塞进 OcrOutcome.error，
    让流水线可以统一处理并落库，不会因为一个坏文件中断整批。

    混合识别策略（仅图片类型）：
      · force_vision=True  ：直接走视觉模型（模板强制）；无视觉模型时回退 RapidOCR。
      · 否则先 RapidOCR    ：若其平均置信度低于阈值（或结果为空/失败），且配置了
        视觉模型，则调用视觉模型升级，用模型文本覆盖。
    PDF 与不支持类型行为不变。
    """
    path = Path(file_path)
    if not path.exists():
        return OcrOutcome(success=False, error=f"文件不存在：{path}")

    ext = path.suffix.lower()

    if ext in settings.OCR_PDF_EXTS:
        return _ocr_pdf(path)

    if ext not in settings.OCR_IMAGE_EXTS:
        return OcrOutcome(success=False, error=f"不支持 OCR 的文件类型：{ext}")

    enabled, threshold = _ocr_vision_policy()
    vision_cfg = _get_vision_model() if (enabled or force_vision) else None

    # 模板强制：无视觉模型则回退 RapidOCR，不中断
    if force_vision:
        if vision_cfg is None:
            return _run_image_rapidocr(path)
        return _run_vision(path, vision_cfg)

    # 默认：先 RapidOCR，仅在低置信/空结果时升级
    rapid = _run_image_rapidocr(path)
    has_text = bool(rapid.text and rapid.text.strip())
    high_conf = rapid.success and has_text and (
        rapid.avg_confidence is None or rapid.avg_confidence >= threshold
    )
    if high_conf:
        return rapid

    if vision_cfg is not None:
        vis = _run_vision(path, vision_cfg)
        if vis.success and vis.text.strip():
            return vis
    return rapid


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
        rapidocr_ok = True
        rapidocr_err = None
    except Exception as e:  # noqa: BLE001
        rapidocr_ok = False
        rapidocr_err = str(e)

    from app.services.kv_store import get_setting
    vision_cfg = _get_vision_model()
    return {
        "available": rapidocr_ok,
        "engine": "rapidocr-onnxruntime",
        "error": rapidocr_err,
        "ocr_vision": {
            "enabled": bool(get_setting("OCR_VISION_ENABLED", settings.OCR_VISION_ENABLED)),
            "model_configured": vision_cfg is not None,
            "model": vision_cfg.model if vision_cfg else None,
        },
    }
