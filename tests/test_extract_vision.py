"""
tests/test_extract_vision.py — 视觉抽取路线 + 双路线对比 的单元测试

覆盖：
  1. _build_vision_prompt 不含 OCR 文本块、含字段清单、要求看图
  2. chat_json_vision 对 Ollama / OpenAI 两种提供方构造正确的多模态请求体
  3. compare_routes 在同一批单据上跑两条路线，正确计算覆盖率与对比结论
"""
from types import SimpleNamespace

from app.models.model_config import ModelConfig
from app.services.extract.extractor import ExtractOutcome, _build_vision_prompt
from app.services.extract.compare import compare_routes
from app.services.llm.client import chat_json_vision


def test_build_vision_prompt_shape():
    tpl = SimpleNamespace(name="送货单", fields_schema=[{"key": "a", "label": "A"}, {"key": "b"}], prompt_extra="")
    p = _build_vision_prompt(tpl)
    assert "送货单" in p
    assert '"a"' in p and '"b"' in p
    assert "图片" in p
    # 视觉路线不应再出现 OCR 文本块（信息来自图片本身）
    assert "OCR 文本如下" not in p


def test_chat_json_vision_ollama_payload(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": '{"fields": {}, "confidence": 0.9}'}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, **k):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr("app.services.llm.client.httpx.Client", FakeClient)
    cfg = ModelConfig(provider="ollama", base_url="http://x", model="vis")
    out = chat_json_vision(cfg, "BASE64IMG", "PROMPT", system="SYS")

    assert captured["url"].endswith("/api/chat")
    msg = captured["json"]["messages"][-1]
    assert msg["images"] == ["BASE64IMG"]
    assert out == {"fields": {}, "confidence": 0.9}


def test_chat_json_vision_openai_payload(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"fields": {}, "confidence": 1}'}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None, **k):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr("app.services.llm.client.httpx.Client", FakeClient)
    cfg = ModelConfig(provider="openai", base_url="http://api.x/v1", api_key="k", model="gpt")
    out = chat_json_vision(cfg, "B64", "P", system="S", image_media_type="image/png")

    assert "chat/completions" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer k"
    content = captured["json"]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out == {"fields": {}, "confidence": 1}


def test_compare_routes_structure(monkeypatch):
    class FakeTemplate:
        name = "送货单"
        fields_schema = [{"key": "a"}, {"key": "b"}]
        prompt_extra = ""

    monkeypatch.setattr(
        "app.services.extract.compare.ocr_engine.recognize",
        lambda path: SimpleNamespace(success=True, text="送货单 供应商X"),
    )
    monkeypatch.setattr(
        "app.services.extract.compare.templates.match_template",
        lambda db, text, ext: FakeTemplate(),
    )
    monkeypatch.setattr(
        "app.services.extract.compare.get_model_for_role",
        lambda role, fallback=False: SimpleNamespace(name="v", model="vl"),
    )

    def fake_extract(tpl, text):
        return ExtractOutcome(success=True, fields={"a": 1, "b": None}, confidence=0.9, model="m", duration_ms=10)

    def fake_vision(tpl, path, role="extract_vision"):
        return ExtractOutcome(success=True, fields={"a": 1, "b": 2}, confidence=0.8, model="vl", duration_ms=20)

    monkeypatch.setattr("app.services.extract.compare.extractor.extract", fake_extract)
    monkeypatch.setattr("app.services.extract.compare.extractor.extract_vision", fake_vision)
    monkeypatch.setattr(
        "app.services.extract.compare._pick_attachments",
        lambda db, attachment_ids=None, sample_size=5: [
            {"doc_id": "1", "name": "d1", "local_path": "x.png", "file_ext": ".png"}
        ],
    )
    monkeypatch.setattr("app.services.extract.compare._generate_samples", lambda n, d: [])

    res = compare_routes(None, sample_size=1, generate_if_empty=False)
    assert res["summary"]["doc_count"] == 1
    det = res["details"][0]
    assert det["a"]["coverage"] == 0.5  # 2 个字段抽中 1 个
    assert det["b"]["coverage"] == 1.0  # 2 个字段抽中 2 个
    assert det["note"] == "视觉路线覆盖更全"


def test_compare_routes_no_vision_model(monkeypatch):
    """未配置视觉模型时，路线 B 整体跳过但路线 A 仍正常。"""
    monkeypatch.setattr(
        "app.services.extract.compare.ocr_engine.recognize",
        lambda path: SimpleNamespace(success=True, text="送货单 供应商X"),
    )
    monkeypatch.setattr(
        "app.services.extract.compare.templates.match_template",
        lambda db, text, ext: SimpleNamespace(name="送货单", fields_schema=[{"key": "a"}], prompt_extra=""),
    )
    monkeypatch.setattr("app.services.extract.compare.get_model_for_role", lambda role, fallback=False: None)
    monkeypatch.setattr(
        "app.services.extract.compare.extractor.extract",
        lambda tpl, text: ExtractOutcome(success=True, fields={"a": 1}, confidence=0.9, model="m", duration_ms=10),
    )
    monkeypatch.setattr(
        "app.services.extract.compare._pick_attachments",
        lambda db, attachment_ids=None, sample_size=5: [
            {"doc_id": "1", "name": "d1", "local_path": "x.png", "file_ext": ".png"}
        ],
    )
    monkeypatch.setattr("app.services.extract.compare._generate_samples", lambda n, d: [])

    res = compare_routes(None, sample_size=1, generate_if_empty=False)
    assert res["summary"]["vision_available"] is False
    assert res["details"][0]["a"]["ok"] is True
    assert res["details"][0]["b"]["ok"] is False
    assert "未配置视觉抽取模型" in (res["details"][0]["b"]["error"] or "")
