"""
tests/test_ocr_vision_hybrid.py — OCR 混合识别（RapidOCR + 视觉模型升级）单元测试

覆盖：
  1. RapidOCR 高置信度 → 直接用 RapidOCR，不调视觉模型（省成本）
  2. RapidOCR 低置信度 → 自动升级到视觉模型，结果 engine=vision-ocr
  3. RapidOCR 结果为空/失败 → 视觉模型兜底
  4. force_vision=True → 直接走视觉模型
  5. 视觉模型不可用（未配置）→ 回退 RapidOCR，不中断
"""
from types import SimpleNamespace

from app.services.ocr import engine as ocr_engine
from app.services.ocr.engine import OcrBlock, OcrOutcome


def _fake_vision_cfg():
    return SimpleNamespace(name="glm-4v", provider="openai", model="glm-4v", base_url="http://x", api_key="k")


def _patch_vision(monkeypatch, vision_text="视觉模型识别出的文字"):
    """把引擎的视觉相关内部函数全部打桩，避免真实网络与 DB。"""
    monkeypatch.setattr(ocr_engine, "_ocr_vision_policy", lambda: (True, 0.6))
    monkeypatch.setattr(ocr_engine, "_get_vision_model", lambda: _fake_vision_cfg())
    monkeypatch.setattr(ocr_engine, "_encode_image", lambda path: ("BASE64", "image/png"))
    monkeypatch.setattr(ocr_engine, "_vision_recognize_text", lambda b64, mt, cfg: vision_text)


def _patch_rapidocr(monkeypatch, blocks, avg):
    monkeypatch.setattr(
        ocr_engine, "_run_rapidocr",
        lambda image_input, page=1: (blocks, avg),
    )


def test_high_confidence_keeps_rapidocr(monkeypatch, tmp_path):
    """高置信度时不应调用视觉模型，结果来自 RapidOCR。"""
    img = tmp_path / "a.png"
    img.write_bytes(b"fake")
    calls = {"vision": 0}
    _patch_vision(monkeypatch, vision_text="模型文本")
    monkeypatch.setattr(ocr_engine, "_vision_recognize_text",
                        lambda b64, mt, cfg: calls.__setitem__("vision", calls["vision"] + 1) or "模型文本")
    _patch_rapidocr(monkeypatch, [OcrBlock(text="清晰中文", score=0.97)], 0.97)

    out = ocr_engine.recognize(img)
    assert out.engine == "rapidocr"
    assert out.text == "清晰中文"
    assert calls["vision"] == 0  # 关键：没花视觉 token


def test_low_confidence_upgrades_to_vision(monkeypatch, tmp_path):
    """低置信度（手写/模糊）自动升级到视觉模型。"""
    img = tmp_path / "b.png"
    img.write_bytes(b"fake")
    _patch_vision(monkeypatch, vision_text="模型还原的手写内容")
    _patch_rapidocr(monkeypatch, [OcrBlock(text="模", score=0.4)], 0.4)

    out = ocr_engine.recognize(img)
    assert out.engine == "vision-ocr"
    assert out.text == "模型还原的手写内容"
    assert out.avg_confidence is None  # 模型不返回坐标置信度


def test_empty_rapidocr_falls_back_to_vision(monkeypatch, tmp_path):
    """RapidOCR 没认出字（空结果）时，视觉模型兜底。"""
    img = tmp_path / "c.png"
    img.write_bytes(b"fake")
    _patch_vision(monkeypatch, vision_text="模型看到的全部文字")
    _patch_rapidocr(monkeypatch, [], None)  # 空 + 无置信度

    out = ocr_engine.recognize(img)
    assert out.engine == "vision-ocr"
    assert out.text == "模型看到的全部文字"


def test_force_vision_uses_model(monkeypatch, tmp_path):
    """force_vision=True 直接走视觉模型，跳过 RapidOCR 阈值判断。"""
    img = tmp_path / "d.png"
    img.write_bytes(b"fake")
    _patch_vision(monkeypatch, vision_text="强制视觉结果")
    _patch_rapidocr(monkeypatch, [OcrBlock(text="x", score=0.99)], 0.99)  # 即便高置信也强制

    out = ocr_engine.recognize(img, force_vision=True)
    assert out.engine == "vision-ocr"
    assert out.text == "强制视觉结果"


def test_vision_unavailable_falls_back_to_rapidocr(monkeypatch, tmp_path):
    """视觉模型未配置时，force_vision 也应回退 RapidOCR 而非报错中断。"""
    img = tmp_path / "e.png"
    img.write_bytes(b"fake")
    monkeypatch.setattr(ocr_engine, "_ocr_vision_policy", lambda: (True, 0.6))
    monkeypatch.setattr(ocr_engine, "_get_vision_model", lambda: None)  # 未配置
    _patch_rapidocr(monkeypatch, [OcrBlock(text="本地OCR结果", score=0.3)], 0.3)  # 低置信但无视觉可用

    out = ocr_engine.recognize(img, force_vision=True)
    assert out.engine == "rapidocr"
    assert out.text == "本地OCR结果"


def test_vision_failure_keeps_rapidocr(monkeypatch, tmp_path):
    """视觉模型调用失败（返回空/异常）时，保留 RapidOCR 结果。"""
    img = tmp_path / "f.png"
    img.write_bytes(b"fake")
    _patch_vision(monkeypatch, vision_text="")  # 模型返回空
    _patch_rapidocr(monkeypatch, [OcrBlock(text="快速棕色狐狸", score=0.35)], 0.35)

    out = ocr_engine.recognize(img)
    # 低置信触发升级，但视觉返回空 → 回退 RapidOCR
    assert out.engine == "rapidocr"
    assert out.text == "快速棕色狐狸"


def test_pdf_and_unsupported_unchanged(monkeypatch, tmp_path):
    """PDF 与不支持类型不应走到视觉升级分支（行为保持原样）。"""
    pdf = tmp_path / "g.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(ocr_engine, "_ocr_pdf", lambda path: OcrOutcome(success=True, text="PDF文本", engine="pdf-textlayer"))
    out = ocr_engine.recognize(pdf)
    assert out.engine == "pdf-textlayer"

    bad = tmp_path / "h.zip"
    bad.write_bytes(b"PK")
    out2 = ocr_engine.recognize(bad)
    assert out2.success is False
    assert "不支持" in (out2.error or "")
